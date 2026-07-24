# kubeadm으로 3노드 Kubernetes 클러스터를 구성하며 알게 된 것들

이전 글에서는 미니 PC에 Proxmox를 설치하고 Ubuntu VM 3대를 만들었다.

이번에는 준비한 VM을 각각 Control Plane과 Worker Node로 구성하고,
`kubeadm`을 사용해 Kubernetes 클러스터로 묶어 보았다.

처음에는 Kubernetes를 설치하려면 각 VM에 Docker부터 설치해야 한다고
생각했다. 하지만 실제로 구성해 보니 Kubernetes, containerd, kubelet,
Calico가 서로 다른 역할을 담당하고 있었다.

명령어만 복사하면 나중에 다시 봐도 이해하지 못할 것 같았다.
그래서 이번 글은 설치 명령보다 **각 설정이 왜 필요한지**를 중심으로
정리해 보려고 한다.

> 이 글은 Ubuntu 22.04 VM과 Kubernetes v1.36 계열을 사용한 개인 학습
> 환경을 기준으로 작성했다. 저장소 주소와 설치 방법은 버전에 따라 달라질 수
> 있으므로 다른 환경에서 그대로 실행하기 전에는 공식 문서를 확인해야 한다.

---

## 1. 구성하려는 클러스터

이번에 구성한 VM의 역할과 IP는 다음과 같다.

| 호스트 | IP | 역할 |
| --- | --- | --- |
| `k8s-master` | `192.168.0.12` | Control Plane |
| `k8s-worker1` | `192.168.0.13` | Worker Node |
| `k8s-worker2` | `192.168.0.14` | Worker Node |

전체 구조를 단순하게 표현하면 다음과 같다.

```text
                     kubectl
                        │
                        ▼
              k8s-master / Control Plane
              192.168.0.12
              ├─ kube-apiserver
              ├─ etcd
              ├─ kube-scheduler
              └─ kube-controller-manager
                        │
             ┌──────────┴──────────┐
             ▼                     ▼
       k8s-worker1            k8s-worker2
       192.168.0.13           192.168.0.14
       ├─ kubelet             ├─ kubelet
       ├─ containerd          ├─ containerd
       └─ kube-proxy          └─ kube-proxy
```

Control Plane은 클러스터의 상태를 저장하고 어떤 Pod를 어느 노드에
배치할지 결정한다.

Worker Node는 Control Plane의 결정을 받아 실제 컨테이너를 실행한다.

### 복기해 볼 내용

- Control Plane은 실제 애플리케이션 컨테이너를 반드시 실행해야 할까?
- Worker가 한 대 죽으면 Control Plane은 어떤 결정을 내릴까?

---

## 2. Kubernetes를 구성하는 프로그램들

설치를 진행하면서 가장 먼저 헷갈렸던 것은 `kubeadm`, `kubelet`,
`kubectl`의 차이였다.

이름은 비슷하지만 역할은 전혀 다르다.

### kubeadm

`kubeadm`은 Kubernetes 클러스터를 처음 구성하는 bootstrap 도구다.

```text
kubeadm init
└─ Control Plane 초기화

kubeadm join
└─ Worker Node를 기존 클러스터에 연결
```

`kubeadm`이 항상 실행되는 서버 프로세스는 아니다.
클러스터를 초기화하거나 노드를 추가할 때 사용하는 명령행 도구다.

### kubelet

`kubelet`은 모든 Kubernetes 노드에서 계속 실행되는 에이전트다.

```text
Control Plane
“이 Pod를 이 노드에서 실행해”
        │
        ▼
kubelet
        │ CRI
        ▼
containerd
        │
        ▼
컨테이너 실행
```

kubelet은 현재 노드의 Pod 상태를 확인하고, 원하는 상태와 실제 상태가
다르면 containerd에 컨테이너 생성이나 재시작을 요청한다.

### kubectl

`kubectl`은 사용자가 Kubernetes API Server에 요청을 보내는 CLI다.

```bash
kubectl get nodes
kubectl get pods -A
kubectl describe pod <POD_NAME> -n <NAMESPACE>
```

`kubectl`이 직접 Worker Node에 접속해 컨테이너를 실행하는 것은 아니다.

```text
사용자
  │ kubectl
  ▼
kube-apiserver
  │
  ├─ scheduler
  ├─ controller
  └─ 각 노드의 kubelet
```

### 한 문장으로 정리

```text
kubeadm  → 클러스터를 구성한다.
kubelet  → 노드에서 Pod를 관리한다.
kubectl  → 사용자가 클러스터에 명령한다.
```

---

## 3. Docker 대신 containerd를 사용한 이유

처음에는 Kubernetes를 사용하려면 모든 VM에 Docker Engine을 설치해야
한다고 생각했다.

하지만 현재 Kubernetes는 CRI(Container Runtime Interface)를 통해
containerd 같은 컨테이너 런타임에 직접 요청할 수 있다.

```text
Kubernetes
   │
   ▼
kubelet
   │ CRI
   ▼
containerd
   │
   ▼
runc
   │
   ▼
Linux 컨테이너 프로세스
```

containerd가 담당하는 작업은 다음과 같다.

- Registry에서 컨테이너 이미지 다운로드
- 이미지 레이어 압축 해제 및 저장
- 컨테이너 생성과 삭제
- 컨테이너 시작과 종료
- 실행 상태 관리

Docker Engine 역시 내부에서 containerd를 사용하지만, Kubernetes
노드에서는 Docker Engine 전체를 거치지 않고 containerd를 직접
런타임으로 사용할 수 있다.

그래서 이번 클러스터에서는 다음처럼 역할을 나눴다.

```text
개발 PC
└─ Docker로 애플리케이션 이미지 빌드
        │
        ▼
Container Registry
        │
        ▼
Kubernetes Node
└─ containerd가 이미지 pull 및 컨테이너 실행
```

Docker와 containerd를 같은 서버에 설치하더라도 이미지 저장소는
일반적으로 서로 다르다.

```text
Docker 이미지
└─ /var/lib/docker

Kubernetes containerd 이미지
└─ /var/lib/containerd
```

따라서 `docker image ls`에 보이는 이미지와 Kubernetes가 사용하는 이미지가
항상 같지는 않다.

### 복기해 볼 내용

- Docker와 containerd는 완전히 경쟁 관계일까?
- Kubernetes가 이미지를 받는 노드는 누가 결정할까?

---

## 4. 모든 노드에서 swap 비활성화

Kubernetes를 설치하기 전에 각 VM에서 swap을 비활성화했다.

현재 swap 상태 확인:

```bash
free -h
swapon --show
```

현재 부팅에서 swap 비활성화:

```bash
sudo swapoff -a
```

재부팅 후에도 다시 활성화되지 않도록 `/etc/fstab`의 swap 항목도
확인해야 한다.

```bash
grep -n swap /etc/fstab
```

필요하다면 swap 항목을 주석 처리한다.

swap은 메모리가 부족할 때 디스크 일부를 메모리처럼 사용하는 기능이다.
하지만 Kubernetes가 Pod의 메모리 사용량과 제한을 일관되게 판단하려면
노드의 메모리 동작을 예측할 수 있어야 한다.

이번 학습 환경에서는 kubelet의 기본 동작에 맞춰 swap을 비활성화했다.

> 최신 Kubernetes에는 swap을 제한적으로 사용하는 기능도 존재한다.
> 이 글에서는 기능을 별도로 구성하지 않고 swap을 끄는 일반적인 kubeadm
> 학습 환경으로 진행했다.

---

## 5. 컨테이너용 커널 모듈 설정

각 VM에서 `overlay`와 `br_netfilter` 모듈을 활성화했다.

```bash
sudo modprobe overlay
sudo modprobe br_netfilter
```

재부팅 후에도 자동으로 로드되도록 설정 파일을 작성했다.

```bash
cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
```

### overlay

컨테이너 이미지는 여러 읽기 전용 레이어와 하나의 쓰기 가능한 레이어로
구성된다.

```text
기본 Linux 이미지
  + 설치한 패키지
  + 애플리케이션 파일
  + 컨테이너 변경 내용
          │
          ▼
하나의 파일시스템처럼 사용
```

`overlay`는 이 레이어들을 하나의 파일시스템처럼 합쳐 사용할 수 있도록
돕는 Linux 커널 기능이다.

### br_netfilter

Pod 트래픽은 Linux 브리지 같은 가상 네트워크 장치를 통과할 수 있다.

`br_netfilter`를 활성화하면 브리지를 통과하는 패킷도 iptables 규칙에서
처리할 수 있다.

```text
Pod
 │
 ▼
Linux Bridge
 │ br_netfilter
 ▼
iptables
 │
 ▼
다른 Pod 또는 노드
```

모듈이 정상적으로 로드됐는지 확인:

```bash
lsmod | grep overlay
lsmod | grep br_netfilter
```

---

## 6. Pod 네트워크를 위한 sysctl 설정

각 VM에서 다음 네트워크 설정을 적용했다.

```bash
cat <<'EOF' | sudo tee /etc/sysctl.d/99-kubernetes-cri.conf
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
```

재부팅하지 않고 설정 적용:

```bash
sudo sysctl --system
```

각 설정의 역할은 다음과 같다.

```text
bridge-nf-call-iptables
└─ Linux 브리지를 지나는 IPv4 트래픽을 iptables에서 처리

bridge-nf-call-ip6tables
└─ Linux 브리지를 지나는 IPv6 트래픽을 ip6tables에서 처리

ip_forward
└─ 노드가 한 네트워크의 패킷을 다른 네트워크로 전달
```

Proxmox 호스트에서도 Tailscale 서브넷 라우터를 구성하기 위해
IP forwarding을 켰지만 적용 위치와 목적은 다르다.

```text
Proxmox의 IP forwarding
└─ Tailscale 네트워크와 192.168.0.0/24 내부망 연결

Ubuntu Kubernetes Node의 IP forwarding
└─ Pod 네트워크와 노드 네트워크 사이의 패킷 전달
```

---

## 7. containerd 설치와 cgroup 설정

모든 노드에 containerd를 설치했다.

```bash
sudo apt update
sudo apt install -y containerd
```

containerd 설정 디렉터리 생성:

```bash
sudo mkdir -p /etc/containerd
```

설치된 containerd 버전의 기본 설정을 파일로 저장했다.

```bash
containerd config default \
  | sudo tee /etc/containerd/config.toml \
  > /dev/null
```

이 명령의 흐름:

```text
containerd config default
└─ 기본 설정을 표준 출력으로 생성
           │
           ▼
sudo tee /etc/containerd/config.toml
└─ root 권한으로 파일에 저장
           │
           ▼
/dev/null
└─ 화면에 다시 출력되는 내용은 버림
```

### cgroup이란?

cgroup(Control Group)은 Linux가 프로세스별 CPU와 메모리 사용량을
추적하고 제한하는 기능이다.

```text
Kubernetes
“이 컨테이너는 메모리를 500Mi까지 사용”
        │
        ▼
containerd
        │
        ▼
Linux cgroup
“실제 프로세스 자원을 제한”
```

Ubuntu는 시스템 서비스를 systemd로 관리한다.
kubelet과 containerd가 같은 방식으로 cgroup을 관리하도록 containerd에
다음 값을 설정했다.

```toml
SystemdCgroup = true
```

설정 변경:

```bash
sudo sed -i \
  's/SystemdCgroup = false/SystemdCgroup = true/' \
  /etc/containerd/config.toml
```

확인:

```bash
grep SystemdCgroup /etc/containerd/config.toml
```

적용:

```bash
sudo systemctl restart containerd
sudo systemctl enable containerd
sudo systemctl status containerd
```

정리하면 다음과 같다.

```text
kubelet    → systemd cgroup
containerd → systemd cgroup
```

두 프로그램이 서로 다른 방식으로 같은 컨테이너 자원을 관리하면서 생길 수
있는 문제를 피하기 위한 설정이라고 이해했다.

---

## 8. Kubernetes 패키지 설치

이번 클러스터는 Kubernetes v1.36 계열 저장소를 사용했다.

먼저 패키지 서명 키를 저장할 디렉터리를 준비했다.

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
```

Kubernetes 저장소 키 등록:

```bash
curl -fsSL \
  https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key \
  | sudo gpg --dearmor \
      -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
```

APT 저장소 등록:

```bash
echo \
  'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
```

패키지 설치:

```bash
sudo apt update
sudo apt install -y kubelet kubeadm kubectl
```

의도하지 않은 자동 버전 변경을 막기 위해 hold를 설정했다.

```bash
sudo apt-mark hold kubelet kubeadm kubectl
```

버전 확인:

```bash
kubeadm version
kubelet --version
kubectl version --client
```

현재 클러스터 구성 후 확인한 Kubernetes 버전은 `v1.36.3`이었다.

> Kubernetes는 구성 요소 간 version skew 정책이 있다. 단순히 최신 패키지로
> 각각 업데이트하지 말고 업그레이드 순서와 지원 범위를 확인해야 한다.

---

## 9. Control Plane 초기화

Control Plane으로 사용할 `k8s-master`에서 `kubeadm init`을 실행했다.

```bash
sudo kubeadm init \
  --apiserver-advertise-address=192.168.0.12 \
  --pod-network-cidr=10.244.0.0/16
```

옵션의 의미:

```text
--apiserver-advertise-address
└─ 다른 노드가 Kubernetes API Server에 접속할 master IP

--pod-network-cidr
└─ Pod에 할당할 가상 IP 주소 범위
```

현재 집 내부망은 `192.168.0.0/24`다.
Pod 네트워크까지 같은 주소 범위를 사용하면 라우팅 충돌이 발생할 수 있으므로
Pod CIDR에는 `10.244.0.0/16`을 사용했다.

실제로 생성된 Pod IP도 다음과 같은 형태였다.

```text
10.244.x.x
```

`kubeadm init`은 단순히 프로그램 하나를 실행하는 명령이 아니다.

내부적으로 다음과 같은 작업을 수행한다.

- 사전 조건 검사
- Kubernetes 인증서 생성
- kubeconfig 파일 생성
- etcd 구성
- API Server 구성
- Scheduler와 Controller Manager 구성
- CoreDNS와 kube-proxy 설치
- Worker가 참가할 수 있는 bootstrap token 생성

Control Plane의 static Pod manifest는 다음 경로에 생성된다.

```text
/etc/kubernetes/manifests
```

kubelet은 이 디렉터리를 감시하다가 다음 Control Plane Pod를 실행한다.

```text
etcd
kube-apiserver
kube-controller-manager
kube-scheduler
```

---

## 10. 일반 사용자용 kubeconfig 설정

`kubeadm init` 후 관리자용 kubeconfig는 다음 경로에 생성된다.

```text
/etc/kubernetes/admin.conf
```

이 파일은 root 소유이므로 일반 사용자 `master`가 그대로 사용하기 어렵다.

다음 명령으로 사용자 홈에 복사했다.

```bash
mkdir -p "$HOME/.kube"

sudo cp -i \
  /etc/kubernetes/admin.conf \
  "$HOME/.kube/config"

sudo chown \
  "$(id -u):$(id -g)" \
  "$HOME/.kube/config"
```

이후 일반 사용자로 확인:

```bash
kubectl get nodes
```

여기서 중요한 점은 `sudo kubectl`을 습관적으로 사용하지 않는 것이다.

```text
일반 사용자 master
└─ /home/master/.kube/config 사용

sudo kubectl
└─ root의 환경 사용
   └─ /root/.kube/config가 없다면 연결 실패 가능
```

실제로 `sudo kubectl`을 실행했을 때 kubeconfig를 찾지 못해
`localhost:8080`으로 접속하려는 오류를 경험했다.

---

## 11. Worker Node를 클러스터에 연결

`kubeadm init`이 끝나면 Worker가 사용할 `kubeadm join` 명령이 출력된다.

형태는 다음과 같다.

```bash
sudo kubeadm join 192.168.0.12:6443 \
  --token <BOOTSTRAP_TOKEN> \
  --discovery-token-ca-cert-hash sha256:<CA_CERT_HASH>
```

이 명령을 `k8s-worker1`, `k8s-worker2`에서 각각 실행했다.

토큰과 CA hash는 클러스터 참가를 위한 인증 정보이므로 블로그에는 실제 값을
기록하지 않는다.

join 명령을 잃어버렸거나 token이 만료됐다면 Control Plane에서 새 명령을
생성할 수 있다.

```bash
kubeadm token create --print-join-command
```

Worker에서 join을 마친 뒤 master에서 확인:

```bash
kubectl get nodes -o wide
```

초기에는 노드가 `NotReady`로 보일 수 있다.
노드가 등록됐더라도 Pod 네트워크를 담당하는 CNI가 아직 설치되지 않았기
때문이다.

---

## 12. CNI와 Calico가 필요한 이유

Kubernetes는 Pod를 어느 노드에 배치할지 결정할 수 있지만, 여러 노드에
흩어진 Pod가 어떤 IP를 받고 서로 어떻게 통신할지는 CNI 플러그인이
담당한다.

CNI는 Container Network Interface의 약자다.

```text
Pod A / worker1
      │
      │ Calico Pod Network
      ▼
Pod B / worker2
```

CNI가 설치되지 않은 상태에서는 다음 문제가 발생할 수 있다.

- 노드가 `NotReady`
- CoreDNS가 `Pending`
- Pod 간 통신 불가
- Pod IP 할당 불가

이번 환경에서는 CNI로 Calico를 사용했다.

Calico는 다음 기능을 제공한다.

- Pod IP 할당
- 노드 간 Pod 트래픽 라우팅
- Kubernetes NetworkPolicy 적용
- 네트워크 상태 관찰

---

## 13. Tigera Operator로 Calico 설치

처음에는 `tigera-operator`와 `calico-system`이 별도 네트워크 제품인 줄
알았다.

관계는 다음과 같다.

```text
Tigera Operator
└─ Calico 설치 및 상태 관리
   ├─ calico-node
   ├─ calico-kube-controllers
   ├─ calico-apiserver
   ├─ calico-typha
   ├─ Goldmane
   └─ Whisker
```

이번 환경에서 확인한 Calico 버전은 `v3.32.1`이었다.

Calico CRD와 Tigera Operator 설치:

```bash
kubectl create -f \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.1/manifests/v1_crd_projectcalico_org.yaml

kubectl create -f \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.1/manifests/tigera-operator.yaml
```

기본 custom resource 다운로드:

```bash
curl -fLO \
  https://raw.githubusercontent.com/projectcalico/calico/v3.32.1/manifests/custom-resources.yaml
```

공식 예제의 기본 Pod CIDR이 현재 환경과 다르다면 `kubeadm init`에서 설정한
`10.244.0.0/16`과 일치하도록 수정해야 한다.

예를 들어 파일에서 IP pool CIDR을 확인한다.

```bash
grep -n -A3 -B3 cidr custom-resources.yaml
```

확인 후 적용:

```bash
kubectl create -f custom-resources.yaml
```

> `kubeadm init`의 `--pod-network-cidr`와 Calico IP pool이 서로 다르면
> Pod 네트워크가 정상적으로 동작하지 않을 수 있다. 또한 Pod CIDR이 실제
> LAN 대역과 겹치지 않도록 해야 한다.

---

## 14. Calico 설치 상태 확인

Tigera Operator로 Calico를 설치하면 `TigeraStatus`라는 Custom Resource를
통해 구성요소 상태를 볼 수 있다.

```bash
watch kubectl get tigerastatus
```

정상 상태는 다음과 비슷하다.

```text
NAME         AVAILABLE   PROGRESSING   DEGRADED
apiserver    True        False         False
calico       True        False         False
goldmane     True        False         False
ippools      True        False         False
whisker      True        False         False
```

각 열의 의미:

```text
AVAILABLE=True
└─ 현재 사용 가능한 상태

PROGRESSING=True
└─ 설치 또는 변경이 진행 중

DEGRADED=True
└─ 일부 구성요소에 문제가 있음
```

문제가 있다면 다음처럼 상세 원인을 확인한다.

```bash
kubectl describe tigerastatus calico
kubectl get pods -n calico-system
kubectl get pods -n tigera-operator
```

`tigera-operator` namespace에는 Calico를 관리하는 Operator가 있고,
실제 Calico 구성요소 대부분은 `calico-system` namespace에서 실행된다.

---

## 15. 최종 클러스터 상태 확인

Calico 설치가 끝난 뒤 세 노드가 모두 `Ready`가 됐다.

```bash
kubectl get nodes
```

확인한 결과:

```text
NAME          STATUS   ROLES           VERSION
k8s-master    Ready    control-plane   v1.36.3
k8s-worker1   Ready    <none>          v1.36.3
k8s-worker2   Ready    <none>          v1.36.3
```

Control Plane 구성요소 확인:

```bash
kubectl get pods -n kube-system
```

Calico 확인:

```bash
kubectl get pods -n calico-system
kubectl get pods -n tigera-operator
```

전체 Pod 확인:

```bash
kubectl get pods -A -o wide
```

이 명령은 모든 namespace의 Pod를 한꺼번에 보여준다.
처음에는 출력이 너무 많아 무엇이 애플리케이션이고 무엇이 시스템 Pod인지
구분하기 어려웠다.

namespace별 역할은 다음과 같다.

```text
kube-system
└─ Kubernetes 핵심 구성요소

calico-system
└─ Calico 네트워크 구성요소

tigera-operator
└─ Calico 관리 Operator

default
└─ namespace를 지정하지 않은 일반 리소스의 기본 공간
```

---

## 16. 구성하면서 헷갈렸던 부분

### kubelet이 설치 직후 계속 재시작했다

`kubeadm init`이나 `join`을 실행하기 전 kubelet이 정상 실행되지 않는 것처럼
보일 수 있다.

kubelet은 아직 사용할 클러스터 설정을 받지 못한 상태이므로 재시작을
반복할 수 있다.

설치 직후에는 kubelet 상태 하나만 보고 실패라고 단정하기보다 kubeadm 구성
단계와 로그를 함께 확인해야 한다.

```bash
sudo systemctl status kubelet
sudo journalctl -u kubelet -n 100 --no-pager
```

### 노드가 등록됐는데 NotReady였다

Worker join이 성공해도 CNI가 없으면 노드는 `NotReady`일 수 있다.

```text
kubeadm join 성공
≠
Pod 네트워크 구성 완료
```

Calico 설치와 상태 확인까지 끝나야 Pod 네트워크를 포함한 클러스터가
정상 상태에 도달한다.

### `kubectl`과 `sudo kubectl` 결과가 달랐다

kubectl은 현재 사용자의 kubeconfig를 사용한다.
root와 일반 사용자는 홈 디렉터리와 kubeconfig 경로가 다르다.

권한 문제가 보인다고 무조건 `sudo`를 붙이면 오히려 다른 클러스터 설정을
읽거나 `localhost:8080`으로 접속할 수 있다.

### Docker 이미지와 Kubernetes 이미지가 따로 보였다

Docker Engine과 Kubernetes의 containerd는 서로 다른 이미지 저장소를
사용한다.

```bash
docker image ls
sudo crictl images
sudo ctr -n k8s.io images list
```

어떤 런타임으로 이미지를 받았는지에 따라 확인 명령도 달라진다.

---

## 17. 이번 구성에서 이해한 전체 흐름

처음에는 Kubernetes가 하나의 거대한 프로그램이라고 생각했다.

실제로는 여러 구성요소가 각자의 역할을 담당한다.

```text
사용자
  │
  │ kubectl
  ▼
kube-apiserver
  │
  ├─ etcd에 상태 저장
  ├─ scheduler가 실행 노드 결정
  └─ controller가 원하는 상태 유지
             │
             ▼
          kubelet
             │ CRI
             ▼
         containerd
             │
             ▼
        컨테이너 실행

Pod 네트워크
└─ Calico가 IP 할당과 노드 간 통신 담당
```

Linux에서는 그 아래에서 다음 기능을 제공한다.

```text
cgroup
└─ CPU와 메모리 사용량 관리

overlay
└─ 컨테이너 이미지 레이어 처리

br_netfilter
└─ 브리지 트래픽을 방화벽 규칙과 연결

IP forwarding
└─ 서로 다른 네트워크 사이의 패킷 전달
```

각 명령의 의미를 따로 볼 때는 복잡했지만, 전체 흐름으로 연결하니 왜 설치
전에 커널과 네트워크 설정이 필요했는지 조금 이해할 수 있었다.

---

## 18. 현재까지 진행한 내용

- Ubuntu 22.04 VM 3대 준비
- 모든 노드에서 swap 비활성화
- `overlay`, `br_netfilter` 활성화
- Pod 네트워크용 sysctl 적용
- containerd 설치
- `SystemdCgroup = true` 설정
- kubelet, kubeadm, kubectl 설치
- `k8s-master` Control Plane 초기화
- worker1, worker2 join
- Calico 및 Tigera Operator 설치
- 세 노드 모두 `Ready` 확인
- Kubernetes v1.36.3 동작 확인

현재는 Kubernetes 클러스터 자체만 구성한 상태다.

다음에는 `sock-shop` 예제 애플리케이션을 배포하면서 다음 내용을 공부해
보려고 한다.

- Kubernetes YAML 구조
- Namespace
- Deployment
- ReplicaSet
- Pod
- Service
- `kubectl describe`
- `kubectl logs`

---

## 19. 글을 작성하면서 직접 답해 볼 질문

아래 질문에는 명령어를 보지 않고 직접 답해 보는 것을 목표로 한다.

1. Control Plane과 Worker Node의 역할은 어떻게 다른가?
2. `kubeadm`, `kubelet`, `kubectl`은 각각 무엇을 하는가?
3. Kubernetes 노드에 Docker Engine이 없어도 되는 이유는 무엇인가?
4. CRI는 왜 필요한가?
5. containerd와 runc는 어떤 관계인가?
6. swap을 비활성화한 이유는 무엇인가?
7. `overlay`와 `br_netfilter`는 각각 어디에 사용되는가?
8. IP forwarding은 Proxmox와 Kubernetes 노드에서 목적이 어떻게 다른가?
9. `SystemdCgroup = true`는 왜 설정했는가?
10. `kubeadm init`은 어떤 구성요소를 생성하는가?
11. kubeconfig는 무엇이며 왜 일반 사용자 홈으로 복사했는가?
12. Worker Node는 어떤 방식으로 Control Plane을 신뢰하고 join하는가?
13. CNI를 설치하지 않으면 왜 노드가 `NotReady`일 수 있는가?
14. Calico와 Tigera Operator는 어떤 관계인가?
15. Pod CIDR과 집 내부 LAN 대역이 겹치면 왜 문제가 되는가?

---

## 참고 문서

- [Kubernetes 공식 문서 - Installing kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/)
- [Kubernetes 공식 문서 - Creating a cluster with kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/create-cluster-kubeadm/)
- [Kubernetes 공식 문서 - kubeadm init](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-init/)
- [Calico 공식 문서 - Quickstart](https://docs.tigera.io/calico/latest/getting-started/kubernetes/quickstart)

---

## 게시 전 내가 확인할 내용

- [ ] Proxmox에서 세 VM의 실제 디스크 크기 다시 확인
- [ ] 각 VM의 CPU와 RAM을 실제 화면 기준으로 기록
- [ ] `kubeadm init`에 사용한 실제 Pod CIDR 확인
- [ ] Calico `custom-resources.yaml`에서 설정한 실제 CIDR 확인
- [ ] Kubernetes 설치 명령이 shell history와 일치하는지 확인
- [ ] `kubectl get nodes -o wide` 스크린샷 추가
- [ ] `kubectl get tigerastatus` 스크린샷 추가
- [ ] bootstrap token과 인증서 hash가 스크린샷에 노출되지 않았는지 확인
- [ ] 각 절의 “복기해 볼 내용”을 내 말로 다시 작성

`Kubernetes` `kubeadm` `containerd` `Calico` `Proxmox` `홈서버`
