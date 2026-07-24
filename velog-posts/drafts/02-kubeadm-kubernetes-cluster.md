# Proxmox VM 3대에 Kubernetes 클러스터를 올렸다

이전 글에서는 미니 PC에 Proxmox를 설치하고 Ubuntu VM 3대를 만들었다.

이번 글은 그다음 과정이다. 준비한 VM에 containerd와 Kubernetes를 설치하고,
Control Plane 1대와 Worker 2대를 하나의 클러스터로 묶었다.

실제로 진행한 순서는 다음과 같다.

1. 세 VM의 역할과 IP 확인
2. swap, 커널 모듈, 네트워크 설정
3. containerd와 Kubernetes 패키지 설치
4. Control Plane 초기화
5. Worker Node 연결
6. Calico 설치
7. 세 노드의 최종 상태 확인

작업 중에는 Docker가 꼭 필요한지, CRI와 cgroup은 무엇인지,
join이 성공했는데 왜 노드가 `NotReady`인지처럼 모르는 부분이 계속 나왔다.
이 글에서는 질문과 답을 별도 문답으로 분리하지 않고, 실제 작업이 진행된
순서 안에 함께 정리했다.

> 환경: Ubuntu 22.04, Kubernetes v1.36.3, containerd, Calico v3.32.1
>
> 구축 과정에서는 생성형 AI의 도움을 받아 명령어와 오류 원인을 확인했다.
> 다만 명령은 직접 실행했고, 결과를 확인하면서 이해한 내용을 이 글에
> 다시 정리했다.

---

## 1. VM 세 대의 역할을 먼저 정했다

| 호스트 | IP | 역할 |
| --- | --- | --- |
| `k8s-master` | `192.168.0.12` | Control Plane |
| `k8s-worker1` | `192.168.0.13` | Worker |
| `k8s-worker2` | `192.168.0.14` | Worker |

Control Plane은 클러스터의 상태를 관리하고 Pod를 어느 노드에 배치할지
결정한다. Worker는 실제 애플리케이션 Pod가 실행되는 공간이다.

이번 구성은 학습과 KUBEIN 실험을 위한 단일 Control Plane 환경이다.
`k8s-master`가 중단되면 기존 컨테이너가 즉시 모두 사라지는 것은 아니지만,
API 요청과 새로운 스케줄링 같은 클러스터 관리 기능은 사용할 수 없게 된다.
따라서 고가용성을 갖춘 운영용 구성과는 다르다.

여기서 **Node**는 Kubernetes 클러스터에 참가한 물리 서버나 VM을 뜻한다.
**Pod**는 Kubernetes가 배포하고 관리하는 가장 작은 실행 단위로, 하나
이상의 컨테이너와 네트워크·저장소 설정을 묶는다. 이번 글에서는 세 VM이
Node가 되고, 다음 글부터 그 위에 애플리케이션 Pod를 올리게 된다.

```text
Control Plane
├─ API Server
├─ Scheduler
├─ Controller Manager
└─ etcd

Worker
├─ kubelet
├─ containerd
└─ Pod
```

Control Plane 안의 이름도 처음에는 전부 비슷하게 보였다.

| 구성요소 | 하는 일 |
| --- | --- |
| API Server | kubectl, kubelet 등 모든 구성요소의 요청을 받는 Kubernetes API의 입구 |
| etcd | 클러스터의 설정과 현재 상태를 저장하는 key-value 저장소 |
| Scheduler | 아직 실행 노드가 정해지지 않은 Pod를 보고 적절한 Node를 선택 |
| Controller Manager | 원하는 상태와 실제 상태를 비교하고 차이를 줄이는 controller들을 실행 |
| kubelet | 각 Node에서 배정된 Pod가 실제로 실행되도록 관리 |
| containerd | 이미지와 컨테이너의 생성·시작·종료를 관리하는 컨테이너 런타임 |

> 📷 **이미지 1 삽입 위치**
>
> 여기에는 [Kubernetes Cluster Architecture 공식 SVG](https://kubernetes.io/images/docs/kubernetes-cluster-architecture.svg)를
> 내려받아 넣는다. Control Plane과 Worker의 차이를 설명한 직후라 독자가
> 실제 Kubernetes 구성요소를 한 번에 연결해서 보기 좋다.
>
> 캡션: *그림 1. Kubernetes 클러스터 구성요소. 출처: [Kubernetes 공식 문서](https://kubernetes.io/docs/concepts/architecture/)*

---

## 2. 클러스터를 구성하고 실행하는 세 도구

처음에는 이름이 비슷해서 세 프로그램의 역할이 가장 헷갈렸다.
설치하면서 확인해 보니 실행되는 시점과 대상부터 서로 달랐다.

- `kubeadm`: 클러스터를 처음 만들거나 노드를 참가시키는 도구
- `kubelet`: 각 노드에서 Pod 상태를 관리하는 서비스
- `kubectl`: 사용자가 Kubernetes API에 명령을 보내는 도구

즉, `kubeadm`으로 클러스터를 만들고, `kubectl`로 명령을 내리면,
각 노드의 `kubelet`이 실제 작업을 수행한다.

### kubeadm은 클러스터 bootstrap 도구다

`kubeadm`은 계속 실행되는 서버가 아니다. 클러스터를 처음 구성하거나
노드를 추가하고 업그레이드할 때 실행하는 bootstrap 도구다.

```text
kubeadm init
└─ Control Plane 초기화

kubeadm join
└─ Worker를 기존 클러스터에 연결
```

### kubelet은 각 노드에 상주한다

`kubelet`은 master와 worker에서 계속 실행되는 systemd 서비스다. systemd는
Ubuntu에서 백그라운드 서비스의 시작·중지·자동 실행을 관리하는 프로그램이다.
API Server로부터 현재 노드에 배정된 Pod 정보를 받고, 실제 상태가 원하는
상태와 다르면 containerd에 컨테이너 생성이나 재시작을 요청한다.

```text
Control Plane
“이 Pod를 worker1에서 실행”
        ↓
worker1의 kubelet
        ↓ CRI
containerd
        ↓
컨테이너 실행
```

### kubectl은 API를 사용하는 클라이언트다

`kubectl`이 Worker에 직접 SSH로 접속하는 것은 아니다. kubeconfig에 기록된
API Server 주소와 인증 정보를 사용해 요청을 보낸다.

```bash
kubectl get nodes
kubectl get pods -A
kubectl describe node k8s-worker1
```

---

## 3. Docker가 없어도 되는 이유: CRI와 containerd

처음에는 Kubernetes를 사용하려면 모든 VM에 Docker를 먼저 설치해야 한다고
생각했다. 하지만 Kubernetes가 컨테이너를 직접 실행하거나 Docker에만
의존하는 것은 아니었다. kubelet은 CRI라는 표준 인터페이스를 통해
containerd 같은 컨테이너 런타임에 실행을 요청한다.

```text
kubelet → CRI → containerd → runc → 컨테이너
```

각 용어의 관계는 다음과 같이 이해했다.

- **CRI(Container Runtime Interface)**: kubelet과 컨테이너 런타임이
  통신하기 위한 공통 규칙이다.
- **containerd**: 이미지를 내려받아 저장하고, 컨테이너의 생성·실행·종료
  같은 생명주기를 관리하는 고수준 컨테이너 런타임이다.
- **runc**: containerd의 요청을 받아 Linux namespace와 cgroup 등을
  사용해 실제 컨테이너 프로세스를 생성하는 저수준 런타임이다.

즉 CRI는 프로그램이라기보다 인터페이스이고, containerd와 runc는 실제
실행 과정에서 서로 다른 계층을 담당한다.

따라서 Kubernetes 노드에 Docker Engine이 반드시 필요한 것은 아니다.
이번 클러스터에서는 containerd를 런타임으로 사용했다.

이 때문에 Docker로 받은 이미지와 Kubernetes가 받은 이미지가 서로 다르게
보일 수도 있다.

```bash
docker image ls
sudo crictl images
sudo ctr -n k8s.io images list
```

`docker image ls`는 Docker Engine의 저장소를 확인한다. `crictl`과
`ctr -n k8s.io`는 Kubernetes가 사용하는 containerd 쪽 이미지를 서로 다른
인터페이스로 확인할 수 있다.

### master에서 이미지를 받으면 worker에도 생길까?

처음에는 master가 이미지를 받으면 클러스터 전체가 함께 사용하는 줄 알았다.
하지만 컨테이너 이미지는 기본적으로 노드마다 따로 관리된다.

Container Registry는 빌드한 이미지를 저장하고 여러 서버에 배포하기 위한
원격 저장소다. Docker Hub나 GitHub Container Registry가 대표적이며,
각 Worker는 필요한 이미지를 Registry에서 직접 받는다.

```text
개발 PC
└─ Docker로 이미지 빌드
       ↓ push
Container Registry
       ↓ pull
Pod가 배치된 Worker의 containerd
```

master에서 `docker pull`을 실행해도 worker로 이미지가 자동 복사되지 않는다.
Pod가 생성되면 Scheduler가 실행할 노드를 결정하고, 해당 노드의 kubelet과
containerd가 Registry에서 필요한 이미지를 받는다.

이때 `imagePullPolicy`에 따라 동작이 달라진다.

| 값 | 동작 |
| --- | --- |
| `IfNotPresent` | 노드에 이미지가 없을 때 받는다 |
| `Always` | Pod를 시작할 때 Registry의 이미지 정보를 확인한다 |
| `Never` | Registry에서 받지 않고 노드의 로컬 이미지만 사용한다 |

따라서 여러 Worker에서 동일한 애플리케이션을 실행하려면 이미지를 직접
복사하기보다 Docker Hub 같은 Registry를 사용하는 편이 관리하기 쉽다.

---

## 4. 세 노드에 공통으로 적용한 사전 설정

설정은 master와 worker 세 노드에 모두 적용했다.
처음에는 설치 문서에 나온 명령을 그대로 실행했지만, 다시 보니 swap,
커널 모듈, IP forwarding은 각각 메모리 관리와 컨테이너 파일시스템,
Pod 네트워크를 준비하는 과정이었다.

### swap 비활성화

swap은 물리 메모리가 부족할 때 디스크 일부를 메모리처럼 사용하는 Linux
기능이다. 메모리보다 느리지만 순간적인 메모리 부족을 완화할 수 있다.

현재 상태를 먼저 확인했다.

```bash
free -h
swapon --show
```

이번 부팅에서 swap을 끄는 명령은 다음과 같다.

```bash
sudo swapoff -a
```

재부팅 후에도 비활성화되도록 `/etc/fstab`의 swap 항목도 주석 처리했다.

```bash
grep -n swap /etc/fstab
```

Linux 노드의 kubelet은 기본적으로 swap이 활성화되어 있으면 시작하지 않는다.
최신 Kubernetes는 별도 설정을 통해 제한적으로 swap을 사용할 수도 있지만,
이번 학습 환경에서는 해당 기능을 구성하지 않고 기본 동작에 맞춰 swap을
비활성화했다.

즉, “Kubernetes는 언제나 swap을 사용할 수 없다”라기보다 이번 구성에서는
추가 변수를 줄이기 위해 사용하지 않았다고 이해하는 것이 정확하다.

### 커널 모듈

```bash
sudo modprobe overlay
sudo modprobe br_netfilter
```

- `overlay`: 컨테이너 이미지의 레이어 파일 시스템에 사용
- `br_netfilter`: 브리지 네트워크 트래픽에 방화벽 규칙을 적용

컨테이너 이미지는 기본 이미지, 패키지, 애플리케이션 파일처럼 여러
레이어로 구성된다. `overlay`는 이 레이어를 하나의 파일시스템처럼 보이게
한다.

`br_netfilter`는 Pod 트래픽이 Linux 브리지를 통과할 때도 iptables 규칙의
처리 대상이 될 수 있도록 한다.

```text
Pod
 ↓
Linux Bridge
 ↓ br_netfilter
iptables
 ↓
다른 Pod 또는 Node
```

`modprobe`로 불러온 상태는 재부팅 후 사라질 수 있으므로 자동 로드 파일에도
기록했다.

```bash
cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
```

현재 로드 상태 확인:

```bash
lsmod | grep overlay
lsmod | grep br_netfilter
```

### 네트워크 설정

처음에는 `sysctl -w`로 현재 부팅에 설정을 적용했다.

IP forwarding은 Linux가 한 네트워크 인터페이스로 들어온 패킷을 다른
인터페이스로 전달하게 하는 기능이다. Kubernetes Node에서는 Pod 네트워크와
Node 네트워크 사이의 패킷 전달에 필요하다.

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=1
```

Pod 트래픽이 노드와 네트워크 사이를 이동할 수 있도록 IP forwarding과
브리지 필터링을 활성화했다.

재부팅 후에도 유지하도록 파일에 저장하고 다시 적용했다.

```bash
cat <<'EOF' | sudo tee /etc/sysctl.d/99-kubernetes-cri.conf
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF

sudo sysctl --system
```

Proxmox에서도 Tailscale 서브넷 라우터를 만들 때 IP forwarding을 켰지만
목적은 서로 다르다.

```text
Proxmox
└─ Tailscale 네트워크와 집 내부망 사이의 패킷 전달

Kubernetes Node
└─ Pod 네트워크와 Node 네트워크 사이의 패킷 전달
```

---

## 5. containerd와 Kubernetes 패키지 설치

containerd를 설치한 뒤 기본 설정 파일을 생성했다.

```bash
sudo apt update
sudo apt install -y containerd

sudo mkdir -p /etc/containerd
containerd config default |
  sudo tee /etc/containerd/config.toml
```

`/etc/containerd/config.toml`에서 다음 값을 사용했다.

```toml
SystemdCgroup = true
```

containerd와 kubelet이 같은 systemd cgroup 체계를 사용하게 하기 위한
설정이다.

cgroup은 프로세스가 사용할 CPU와 메모리 같은 자원을 관리하는 Linux
기능이다. Kubernetes에서 Pod에 resource request나 limit을 지정하면
실제 제한은 이 계층에서 적용된다.

kubelet과 containerd가 서로 다른 cgroup driver를 사용하면 동일한
프로세스와 자원을 서로 다르게 해석할 수 있다. Ubuntu가 systemd를
사용하므로 두 구성요소도 systemd 방식으로 맞췄다.

containerd를 Kubernetes에서 사용하려면 CRI 기능도 활성화되어 있어야 한다.
`config.toml`의 `disabled_plugins`에 `cri`가 들어 있지 않은지 확인했다.

```bash
grep -n "disabled_plugins" /etc/containerd/config.toml
grep -n "SystemdCgroup" /etc/containerd/config.toml
```

```bash
sudo systemctl restart containerd
sudo systemctl enable containerd
```

이후 Kubernetes 공식 저장소를 등록하고 `kubelet`, `kubeadm`,
`kubectl`을 설치했다.

이번 환경은 v1.36 계열 저장소를 사용했다.

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings

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

sudo apt update
```

패키지를 설치하기 전에 세 노드가 같은 minor 버전의 저장소를 바라보는지
확인했다. Control Plane과 Worker의 구성요소를 아무 순서로나 최신 버전으로
올리면 version skew 문제가 생길 수 있기 때문이다.

```bash
sudo apt install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

`apt-mark hold`는 일반 패키지 업데이트 중 Kubernetes 구성요소의 버전이
의도치 않게 바뀌는 것을 막는다.

설치 후 버전 확인:

```bash
kubeadm version
kubelet --version
kubectl version --client
```

> 저장소 등록 명령은 Kubernetes 버전에 따라 달라질 수 있으므로 실제
> 설치할 때는 공식 문서를 확인하는 것이 안전하다.

---

## 6. Control Plane 초기화

`k8s-master`에서 다음과 같은 형태로 클러스터를 초기화했다.
처음에는 단순히 master를 등록하는 명령으로 생각했지만, 실제로는 인증서,
etcd, API Server, Scheduler 같은 Control Plane 구성요소를 함께 만드는
작업이었다.

```bash
sudo kubeadm init \
  --apiserver-advertise-address=192.168.0.12 \
  --pod-network-cidr=10.244.0.0/16
```

- `--apiserver-advertise-address`: Worker가 접속할 API Server 주소
- `--pod-network-cidr`: Pod에 할당할 가상 IP 범위

집 내부망인 `192.168.0.0/24`와 Pod 네트워크가 겹치지 않도록 서로 다른
대역을 사용했다.

실제로 생성된 Pod IP도 `10.244.x.x` 형태였다.

`kubeadm init`은 명령 하나로 다음 작업을 묶어서 처리한다.

- 노드와 런타임의 사전 조건 검사
- 인증서와 kubeconfig 생성
- etcd 구성
- API Server 구성
- Scheduler와 Controller Manager 구성
- CoreDNS와 kube-proxy 설치
- Worker가 사용할 bootstrap token 생성

여기서 **Service**는 여러 Pod에 안정적으로 접근하기 위한 고정 진입점이다.
**CoreDNS**는 Service 이름을 클러스터 내부 IP로 찾을 수 있게 해주는 DNS
서버다. **kube-proxy**는 각 Node에서 Service 트래픽이 실제 Pod로 전달되도록
네트워크 규칙을 관리한다.

따라서 둘 다 애플리케이션 Pod가 다른 Pod나 Service와 통신하는 데 필요한
기본 구성요소다.

Control Plane 구성요소는 다음 경로에 static Pod manifest로 생성된다.

```text
/etc/kubernetes/manifests
```

kubelet은 이 디렉터리를 감시하면서 다음 Pod를 실행한다.

```text
etcd
kube-apiserver
kube-controller-manager
kube-scheduler
```

초기화 직후에는 CNI가 없기 때문에 CoreDNS가 바로 정상화되지 않을 수 있다.
따라서 `kubeadm init` 성공과 Pod 네트워크 완성은 별개의 단계다.

초기화가 끝난 뒤 일반 사용자도 `kubectl`을 사용할 수 있도록 kubeconfig를
복사했다.

```bash
mkdir -p "$HOME/.kube"
sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
```

kubeconfig에는 단순한 IP 주소만 들어 있는 것이 아니다.

```text
cluster
└─ 접속할 API Server와 CA 정보

user
└─ 사용자 인증 정보

context
└─ 어떤 cluster와 user 조합을 사용할지 지정
```

여기서 `sudo kubectl`을 습관적으로 사용하면 안 된다. 일반 사용자와 root는
서로 다른 kubeconfig를 읽기 때문에, root 설정이 없다면
`localhost:8080 connection refused` 오류가 발생할 수 있다.

---

## 7. 두 Worker를 클러스터에 연결

`kubeadm init` 결과로 출력된 join 명령을 두 Worker에서 실행했다.
명령에 포함된 token과 CA hash가 무엇인지 몰랐는데, Worker가 처음 자신을
등록하고 올바른 API Server에 접속했는지 확인하기 위한 값이었다.

```bash
sudo kubeadm join 192.168.0.12:6443 \
  --token <BOOTSTRAP_TOKEN> \
  --discovery-token-ca-cert-hash sha256:<CA_CERT_HASH>
```

실제 토큰과 인증서 해시는 인증 정보이므로 블로그나 공개 저장소에 올리지
않는다.

명령을 잃어버렸거나 토큰이 만료됐다면 master에서 다시 만들 수 있다.

```bash
kubeadm token create --print-join-command
```

bootstrap token은 Worker가 처음 Control Plane에 자신을 등록할 때 사용하는
임시 인증 정보다. `discovery-token-ca-cert-hash`는 Worker가 접속한 API
Server의 CA 공개키가 예상한 값인지 확인하는 데 사용된다.

```text
Worker
  │ bootstrap token
  │ CA hash 확인
  ▼
API Server
  │
  └─ kubelet 등록과 인증서 발급 과정 진행
```

두 Worker에서 join을 마친 뒤 master에서 확인했다.

```bash
kubectl get nodes -o wide
```

이 시점에 노드 이름이 보인다면 Control Plane 등록은 성공한 것이다.
다만 CNI를 아직 설치하지 않았다면 상태가 `NotReady`여도 이상하지 않다.

join이 실패하면 Worker에서 다음 로그를 먼저 확인할 수 있다.

```bash
sudo systemctl status kubelet
sudo journalctl -u kubelet -n 100 --no-pager
```

---

## 8. CNI로 Calico 설치

Worker가 클러스터에 참가해도 CNI가 없으면 노드는 `NotReady` 상태일 수
있다. Kubernetes는 Pod 배치를 관리하지만, Pod IP 할당과 노드 간 통신은
CNI 플러그인이 담당하기 때문이다.

실제로 join까지 끝났는데 노드가 바로 `Ready`가 되지 않아 이유를 찾아봤고,
아직 Pod 네트워크가 없다는 점을 알게 됐다. 이번 환경에서는 CNI로 Calico를
사용했다.

**CNI(Container Network Interface)**는 컨테이너 네트워크를 구성하기 위한
표준이다. Kubernetes는 이 규칙을 구현한 플러그인에 Pod의 네트워크 설정을
맡긴다.

**Calico**는 CNI를 구현해 Pod IP 할당과 Node 간 라우팅, Pod 사이의
통신 허용·차단 규칙인 NetworkPolicy를 제공하는 네트워크 플러그인이다.

**Tigera Operator**는 Calico 구성요소를 설치하고 원하는 상태로 유지하는
Kubernetes Operator다. 즉 Tigera와 Calico가 서로 경쟁하는 별도 네트워크
제품인 것이 아니라, 이번 구성에서는 Operator가 Calico를 관리한다.

```text
Tigera Operator
└─ Calico 설치와 상태 관리
   ├─ calico-node
   ├─ calico-kube-controllers
   └─ calico-apiserver
```

처음에는 `tigera-operator`가 Calico와 다른 프로그램인 줄 알았다.
실제로는 Calico 구성요소를 설치하고 관리하는 Operator였다.

설치할 때는 `kubeadm init`에서 지정한 Pod CIDR과 Calico의 IP pool CIDR이
일치하는지 확인해야 한다. 또한 이 주소가 실제 LAN 대역과 겹치면 안 된다.

> 📷 **이미지 2 삽입 위치**
>
> 여기에는 [Calico Component Architecture 공식 SVG](https://docs.tigera.io/assets/images/architecture-calico-deae813300e472483f84d6bfb49650ab.svg)를
> 넣는다. 그림의 구성요소가 많으므로 전부 설명하기보다 이번 환경에서 확인한
> `calico-node`, `kube-controllers`, API server를 중심으로 본다.
>
> 캡션: *그림 2. Calico 구성요소. 출처: [Calico 공식 문서](https://docs.tigera.io/calico/latest/reference/architecture/overview)*

상태는 다음 명령으로 확인했다.

```bash
kubectl get tigerastatus
kubectl get pods -n calico-system
kubectl get pods -n tigera-operator
```

`TigeraStatus`는 Kubernetes 기본 리소스가 아니라 Tigera Operator가 추가한
Custom Resource다. 출력은 다음처럼 해석할 수 있다.

```text
AVAILABLE=True
└─ 현재 사용할 수 있는 상태

PROGRESSING=True
└─ 설치 또는 설정 변경이 진행 중

DEGRADED=True
└─ 일부 구성요소에 문제가 있는 상태
```

정상 상태라면 일반적으로 `AVAILABLE=True`, `PROGRESSING=False`,
`DEGRADED=False`가 된다.

### 노드가 NotReady일 때 확인한 순서

Worker의 join이 성공했다고 해서 네트워크 구성까지 끝난 것은 아니다.
노드가 `NotReady`라면 무작정 재설치하기보다 다음 순서로 확인했다.

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get tigerastatus
kubectl describe node <NODE_NAME>
```

확인할 핵심은 다음과 같다.

1. kubelet이 Control Plane에 정상 등록됐는가?
2. Calico Pod가 각 노드에서 실행 중인가?
3. Pod IP가 의도한 CIDR에서 할당됐는가?
4. Node의 `Conditions`와 Events에 네트워크 오류가 있는가?

`NotReady`는 원인 그 자체가 아니라 현재 상태를 나타낸다. 따라서 상태
문자열만 보고 재설치하기보다 Events와 관련 시스템 Pod를 함께 봐야 한다.

---

## 9. 세 노드가 Ready인지 검증

```bash
kubectl get nodes
```

세 노드가 모두 `Ready` 상태가 됐다.

```text
NAME          STATUS   ROLES           VERSION
k8s-master    Ready    control-plane   v1.36.3
k8s-worker1   Ready    <none>          v1.36.3
k8s-worker2   Ready    <none>          v1.36.3
```

> 📷 **이미지 3 삽입 위치**
>
> 이곳에는 직접 실행한 `kubectl get nodes -o wide` 결과 스크린샷을 넣는다.
> 공식 구조도만 사용하는 것보다 실제로 세 노드가 `Ready`가 된 증거가 있어
> 구축 기록으로서 설득력이 높아진다. 토큰, 사용자 이름 등 민감한 값이
> 포함되지 않았는지 확인한 뒤 올린다.

전체 시스템 Pod는 다음 명령으로 확인할 수 있다.

```bash
kubectl get pods -A
```

Control Plane 핵심 Pod만 확인:

```bash
kubectl get pods -n kube-system
```

Calico 구성요소 확인:

```bash
kubectl get pods -n calico-system
kubectl get pods -n tigera-operator
```

출력이 복잡해 보이지만 namespace별로 나누면 이해하기 쉽다.

- `kube-system`: Kubernetes 핵심 구성요소
- `calico-system`: Calico 네트워크 구성요소
- `tigera-operator`: Calico를 관리하는 Operator
- `default`: namespace를 지정하지 않은 일반 리소스

상태 문자열도 구분해서 볼 필요가 있다.

```text
Running
└─ Pod 프로세스가 실행 중

Ready
└─ 트래픽을 받을 준비가 됐다고 판단된 상태

Completed
└─ 작업을 정상적으로 끝낸 Pod

CrashLoopBackOff
└─ 컨테이너가 반복 종료되어 재시작 간격이 늘어난 상태
```

`Running` 하나만 보고 전체 기능이 정상이라고 단정할 수는 없다.
노드, 시스템 Pod, CNI 상태를 함께 확인해야 클러스터 기본 구성이 정상이라고
판단할 수 있다.

---

## 10. 명령이 Pod 실행으로 이어지는 전체 흐름

```text
kubectl
   ↓
API Server
   ↓
Scheduler와 Controller
   ↓
kubelet
   ↓
containerd
   ↓
Pod 실행

Calico
└─ Pod IP 할당과 노드 간 통신 담당
```

Kubernetes를 하나의 거대한 프로그램으로 생각하면 복잡하다.
각 구성요소가 어떤 요청을 받아 누구에게 전달하는지 순서대로 보면 조금씩
구조가 보이기 시작한다.

예를 들어 이후 YAML로 Deployment를 적용하면 다음 순서로 진행된다.

```text
1. kubectl이 YAML을 API Server에 전달
2. API Server가 요청을 검증하고 etcd에 원하는 상태 저장
3. Controller가 필요한 Pod 생성을 결정
4. Scheduler가 실행할 Worker 선택
5. 선택된 Worker의 kubelet이 Pod 정보를 확인
6. containerd가 Registry에서 이미지 pull
7. runc가 컨테이너 프로세스 실행
8. Calico가 Pod IP와 네트워크 구성
```

이 흐름을 이해하고 나니 질문의 범위도 나눌 수 있었다.

```text
Pod가 어느 Node에 배치됐는가?
└─ Scheduler와 리소스 조건 확인

이미지를 받지 못하는가?
└─ Registry, 이미지 이름, containerd 확인

Pod IP를 받지 못하는가?
└─ CNI와 Calico 확인

컨테이너가 실행 후 종료되는가?
└─ 애플리케이션 로그 확인

Node 자체가 불안정한가?
└─ kubelet, 디스크, 메모리, 런타임 확인
```

---

## 11. AI의 도움을 받은 방식

이번 작업에서는 AI에게 설치 순서, 명령어의 의미, 오류 메시지의 원인을
질문했다. 답변을 그대로 실행하기보다는 현재 서버 상태를 다시 확인하고,
명령 실행 결과가 예상과 같은지 비교했다.

궁금한 내용을 마지막에 질문 목록으로 몰아넣으면 실제 진행 과정이 끊겨
보였다. 그래서 Docker와 containerd의 차이는 런타임을 설치한 부분에,
`NotReady`의 이유는 Calico를 설치한 부분에 적는 식으로 작업 과정 안에
질문과 이해한 내용을 함께 남겼다.

AI를 사용하지 않았다고 숨기기보다, **어디까지 도움을 받았고 무엇을 직접
검증했는지 남기는 것**도 이번 공부의 일부라고 생각한다.

---

## 12. 마치며

이번 구성에서 가장 크게 바뀐 생각은 Kubernetes를 하나의 프로그램으로
보지 않게 된 것이다. 클러스터 구성은 kubeadm, 노드의 지속적인 관리는
kubelet, 컨테이너 실행은 containerd, Pod 네트워크는 Calico가 담당했다.

명령이 성공했다는 사실만으로 전체 시스템이 정상인 것도 아니었다.
노드 등록, CNI 상태, 시스템 Pod, 실제 네트워크가 각각 정상인지 단계별로
확인해야 했다.

현재는 세 노드가 `Ready`인 기본 클러스터까지만 완성했다. 다음 글에서는
Sock Shop을 배포하면서 Namespace, Deployment, ReplicaSet, Pod, Service가
어떻게 연결되는지, 그리고 장애 상태를 어떤 순서로 확인하는지 정리할
예정이다.

---

## 초안 작업 메모: 이미지 출처

- [Kubernetes Cluster Architecture](https://kubernetes.io/docs/concepts/architecture/)
- [Calico Component Architecture](https://docs.tigera.io/calico/latest/reference/architecture/overview)

> 실제 이미지를 넣은 뒤에는 본문의 `📷 이미지 삽입 위치` 안내 문구를
> 삭제하고 캡션만 남긴다.

---

## 참고 문서

- [Installing kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/)
- [Creating a cluster with kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/create-cluster-kubeadm/)
- [Container Runtimes](https://kubernetes.io/docs/setup/production-environment/container-runtimes/)
- [Calico Quickstart](https://docs.tigera.io/calico/latest/getting-started/kubernetes/quickstart)

## 게시 전 확인

- 실제 VM 사양
- 실제로 사용한 Pod CIDR과 Calico CIDR
- `kubectl get nodes` 스크린샷
- 토큰과 인증서 해시 노출 여부

