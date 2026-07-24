# Proxmox VM 3대에 Kubernetes 클러스터를 올렸다

이전 글에서는 미니 PC에 Proxmox를 설치하고 Ubuntu VM 3대를 만들었다.

이제 이 VM 3대에 Kubernetes를 올릴 차례였다.

사실 시작할 때는 Docker부터 설치하고 명령어 몇 개만 입력하면 되는 줄
알았다. 막상 해보니 containerd, CRI, CNI처럼 처음 보는 이름이 계속
나왔고, Worker를 연결한 뒤에도 노드가 바로 정상 상태가 되지 않았다.

그래서 이번 글에는 실제로 작업한 순서와 그때마다 헷갈렸던 내용을 같이
적어보려고 한다.

1. 세 VM의 역할과 IP 확인
2. swap, 커널 모듈, 네트워크 설정
3. containerd와 Kubernetes 패키지 설치
4. Control Plane 초기화
5. Worker Node 연결
6. Calico 설치
7. 세 노드의 최종 상태 확인

> 환경: Ubuntu 22.04, Kubernetes v1.36.3, containerd, Calico v3.32.1
>
> 모르는 내용은 생성형 AI에 물어보면서 진행했다. 명령어를 그대로 믿고
> 넘기지는 않고, 서버에서 직접 실행한 결과와 공식 문서를 같이 확인했다.

---

## 1. VM 3대의 역할부터 정했다

| 호스트 | IP | 역할 |
| --- | --- | --- |
| `k8s-master` | `192.168.0.12` | Control Plane |
| `k8s-worker1` | `192.168.0.13` | Worker |
| `k8s-worker2` | `192.168.0.14` | Worker |

`k8s-master`는 클러스터를 관리하는 Control Plane으로, 나머지 두 대는
실제 애플리케이션을 실행하는 Worker로 사용했다.

Control Plane은 클러스터 상태를 관리하고 Pod를 어느 노드에 배치할지
결정한다. Worker는 배정받은 Pod를 실제로 실행한다.

이번에는 공부와 KUBEIN 실험이 목적이라 Control Plane을 한 대만 구성했다.
운영 환경처럼 장애에 대비한 고가용성 구성은 아니다. `k8s-master`가
중단돼도 이미 실행 중인 컨테이너가 바로 전부 사라지는 것은 아니지만,
새로운 명령을 받거나 Pod를 다시 배치하는 관리 기능에는 문제가 생긴다.

여기서 **Node**는 Kubernetes 클러스터에 참가한 서버나 VM을 뜻한다.
이번에는 VM 3대가 각각 하나의 Node가 된다.

**Pod**는 Kubernetes가 관리하는 가장 작은 실행 단위다. 컨테이너 하나만
들어갈 수도 있고, 필요하면 여러 컨테이너가 네트워크와 저장소를 공유할
수도 있다. 일단 지금은 “컨테이너를 감싸서 Kubernetes가 관리하는 단위”
정도로 이해했다.

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

Control Plane 안에도 여러 프로그램이 있었는데 처음에는 이름부터
헷갈렸다. 내가 이해한 역할은 대충 다음과 같다.

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

## 2. kubeadm, kubelet, kubectl부터 구분했다

설치 문서를 보다 보니 `kubeadm`, `kubelet`, `kubectl`이 계속 나왔다.
이름이 비슷해서 처음에는 셋 다 비슷한 설치 도구인 줄 알았다.

막상 하나씩 찾아보니 역할이 전부 달랐다.

- `kubeadm`: 클러스터를 처음 만들거나 노드를 참가시키는 도구
- `kubelet`: 각 노드에서 Pod 상태를 관리하는 서비스
- `kubectl`: 사용자가 Kubernetes API에 명령을 보내는 도구

`kubeadm`으로 클러스터를 만들고, 내가 `kubectl`로 명령을 보내면,
각 Node에서 실행 중인 `kubelet`이 그 명령에 맞춰 Pod 상태를 관리한다.

### kubeadm은 클러스터를 처음 만드는 도구

`kubeadm`은 백그라운드에서 계속 실행되는 프로그램이 아니다. 처음
클러스터를 만들거나 다른 Node를 참가시킬 때 사용하는 도구다.

문서에서는 이런 초기 구성 작업을 **bootstrap**이라고 부른다.

```text
kubeadm init
└─ Control Plane 초기화

kubeadm join
└─ Worker를 기존 클러스터에 연결
```

### kubelet은 각 Node에서 계속 실행된다

`kubelet`은 master와 worker에서 계속 실행되는 systemd 서비스다.
**systemd**는 Ubuntu에서 백그라운드 서비스의 시작, 중지, 자동 실행을
관리한다.

kubelet은 API Server에서 현재 Node에 배정된 Pod 정보를 받아온다. 실제
상태가 원하는 상태와 다르면 containerd에 컨테이너 생성이나 재시작을
요청한다.

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

### kubectl은 내가 명령을 보낼 때 사용한다

`kubectl`은 내가 터미널에서 사용하는 명령어 도구다. 처음에는 이 명령이
Worker에 직접 접속하는 줄 알았는데, 실제로는 kubeconfig에 적힌 API Server
주소로 요청을 보낸다.

```bash
kubectl get nodes
kubectl get pods -A
kubectl describe node k8s-worker1
```

---

## 3. Docker 대신 containerd를 사용했다

여기서 가장 먼저 헷갈린 것이 Docker였다.

나는 Kubernetes로 컨테이너를 실행하려면 모든 VM에 Docker부터 설치해야
한다고 생각했다. 그런데 이번 구성에서는 Docker Engine을 설치하지 않고
containerd를 사용했다.

Kubernetes가 컨테이너를 직접 실행하는 것은 아니다. kubelet이 CRI라는
규칙을 통해 containerd 같은 컨테이너 런타임에 실행을 요청한다.

```text
kubelet → CRI → containerd → runc → 컨테이너
```

내가 이해한 관계는 다음과 같다.

- **CRI(Container Runtime Interface)**: kubelet과 컨테이너 런타임이
  통신하기 위한 공통 규칙이다.
- **containerd**: 이미지를 내려받아 저장하고, 컨테이너의 생성·실행·종료
  같은 생명주기를 관리하는 고수준 컨테이너 런타임이다.
- **runc**: containerd의 요청을 받아 Linux namespace와 cgroup 등을
  사용해 실제 컨테이너 프로세스를 생성하는 저수준 런타임이다.

CRI는 설치해서 실행하는 프로그램이 아니라 kubelet과 런타임 사이의 공통
규칙에 가깝다. containerd는 이미지와 컨테이너를 관리하고, 그 아래의
runc가 실제 Linux 프로세스를 만든다.

결국 Kubernetes Node에 Docker Engine이 꼭 필요한 것은 아니었다. 이번
클러스터에서는 containerd를 바로 런타임으로 사용했다.

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

이것도 처음에는 master가 이미지를 받으면 클러스터 전체에서 같이 쓰는 줄
알았다. 하지만 컨테이너 이미지는 Node마다 따로 저장된다.

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

그래서 여러 Worker에서 같은 애플리케이션을 실행하려면 이미지를 서버마다
직접 복사하는 것보다 Docker Hub 같은 Registry에 올려두는 편이 편하다.

---

## 4. 세 VM에 공통 설정을 넣었다

이제 master와 worker 세 대에 같은 설정을 적용했다.

처음에는 설치 문서에 나온 명령을 거의 그대로 입력했다. 그런데 명령만
남겨두면 나중에 다시 봐도 모를 것 같아서 swap, 커널 모듈, IP forwarding이
왜 필요한지도 같이 찾아봤다.

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

Kubernetes가 무조건 swap을 절대 사용할 수 없는 것은 아니었다. 별도
설정으로 제한적으로 사용할 수도 있지만, 이번에는 처음 구성하는 환경이라
변수를 줄이기 위해 그냥 껐다.

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

먼저 `sysctl -w`로 현재 부팅에 설정을 적용했다.

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

## 5. containerd와 Kubernetes를 설치했다

공통 설정을 마친 뒤 containerd를 설치했다. 설치만 하면 끝인 줄 알았는데
Kubernetes와 같이 쓰려면 설정 파일도 확인해야 했다.

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

여기서 또 처음 보는 것이 cgroup이었다.

**cgroup**은 프로세스가 사용할 CPU와 메모리 같은 자원을 관리하는 Linux
기능이다. 나중에 Pod에 CPU나 메모리 제한을 지정하면 실제 제한은 이
계층에서 적용된다.

`SystemdCgroup = true`는 containerd와 kubelet이 같은 systemd 방식으로
cgroup을 관리하게 맞추는 설정이다. 둘이 서로 다른 방식을 사용하면 같은
프로세스와 자원을 다르게 해석할 수 있다고 해서 Ubuntu의 systemd 방식으로
통일했다.

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

## 6. master에서 kubeadm init을 실행했다

이제 `k8s-master`에서 클러스터를 만들었다.

처음에는 `kubeadm init`이 master를 등록하는 정도의 명령인 줄 알았다.
실제로는 인증서, etcd, API Server, Scheduler 같은 Control Plane 구성요소를
한꺼번에 만드는 작업이었다.

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

CoreDNS와 kube-proxy 모두 애플리케이션 Pod가 다른 Pod나 Service와
통신할 때 필요한 기본 구성요소였다.

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

그런데 `kubeadm init`이 성공했다고 클러스터가 전부 끝난 것은 아니었다.
이 시점에는 아직 CNI가 없어서 CoreDNS가 바로 정상화되지 않을 수 있다.
Pod 네트워크는 다음 단계에서 따로 만들어야 했다.

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

## 7. worker1과 worker2를 join했다

`kubeadm init`이 끝나면 Worker에서 실행할 join 명령이 출력된다. 그 명령을
`k8s-worker1`과 `k8s-worker2`에서 각각 실행했다.

명령 안에 token과 CA hash가 들어 있었는데 처음에는 왜 필요한지 몰랐다.
찾아보니 Worker가 처음 자신을 등록하고, 접속한 API Server가 진짜 내가 만든
서버인지 확인하는 데 사용하는 값이었다.

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

## 8. NotReady를 해결하려고 Calico를 설치했다

join 명령은 성공했는데 `kubectl get nodes`를 실행하니 Worker가
`NotReady`로 나왔다. join이 성공했으니 바로 끝난 줄 알았는데 아니었다.

이유는 아직 CNI가 없어서였다. Kubernetes가 Pod 배치는 관리하지만, Pod
IP를 나눠주고 서로 통신하게 만드는 일은 CNI 플러그인이 맡는다. 이번에는
Calico를 설치했다.

**CNI(Container Network Interface)**는 컨테이너 네트워크를 구성하기 위한
표준이다. Kubernetes는 이 규칙을 구현한 플러그인에 Pod의 네트워크 설정을
맡긴다.

**Calico**는 CNI를 구현해 Pod IP 할당과 Node 간 라우팅, Pod 사이의
통신 허용·차단 규칙인 NetworkPolicy를 제공하는 네트워크 플러그인이다.

설치하고 보니 `tigera-operator`라는 Namespace와 Pod도 생겼다. Calico를
깔았는데 Tigera는 또 뭔가 싶었다.

**Tigera Operator**는 Calico 구성요소를 설치하고 상태를 유지하는
Kubernetes Operator다. Tigera와 Calico가 따로 경쟁하는 프로그램이 아니라,
이번 구성에서는 Tigera Operator가 Calico를 관리하고 있었다.

```text
Tigera Operator
└─ Calico 설치와 상태 관리
   ├─ calico-node
   ├─ calico-kube-controllers
   └─ calico-apiserver
```

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

`NotReady` 자체가 오류의 원인인 것은 아니었다. 그냥 현재 준비되지 않았다는
상태만 보여준다. 원인을 찾으려면 Events와 관련 시스템 Pod를 같이 봐야 했다.

---

## 9. 세 노드가 Ready가 됐다

```bash
kubectl get nodes
```

Calico 설치가 끝난 뒤 세 Node가 모두 `Ready` 상태가 됐다.

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

`kubectl get pods -A` 결과는 처음 보면 꽤 복잡하다. Namespace별로
나눠 보니 그나마 무엇이 Kubernetes 자체 구성요소이고 무엇이 Calico인지
구분할 수 있었다.

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

여기서 `Running`만 보인다고 전부 정상인 것도 아니었다. Node 상태, 시스템
Pod, CNI 상태까지 같이 봐야 기본 구성이 끝났다고 볼 수 있었다.

---

## 10. 이제 전체 흐름이 조금 보였다

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

처음에는 Kubernetes가 그냥 컨테이너를 여러 서버에 띄워주는 하나의 큰
프로그램처럼 보였다. 여기까지 직접 구성하고 나니 여러 구성요소가 요청을
주고받는 구조라는 게 조금 보이기 시작했다.

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

오류가 났을 때 어디부터 봐야 하는지도 이전보다는 조금 구분할 수 있게 됐다.

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

이번 작업은 모르는 것이 나올 때마다 AI에 물어보면서 진행했다.
Docker 없이 Kubernetes를 구성해도 되는지, containerd는 어디에 이미지를
저장하는지, join이 성공했는데 왜 `NotReady`인지 같은 것들을 계속 물었다.

물론 답변을 그대로 복사해서 실행한 것은 아니다. 서버의 현재 상태를 먼저
확인하고, 명령을 실행한 뒤 출력이 예상과 같은지 다시 확인했다. 틀리거나
내 환경과 맞지 않는 답도 있어서 공식 문서도 같이 봤다.

AI를 썼다는 사실을 굳이 숨길 필요는 없다고 생각한다. 대신 무엇을 물었고,
내 서버에서 무엇을 직접 확인했는지는 남겨두려고 한다. 그래야 나중에 다시
봤을 때 내 공부 기록이 된다.

---

## 12. 마치며

사실 시작할 때는 Docker를 설치하고 `kubeadm` 명령 몇 개만 입력하면 끝날
줄 알았다. 막상 해보니 Kubernetes 하나를 설치한 것이 아니라 kubelet,
containerd, Calico 같은 여러 구성요소를 연결한 것이었다.

명령 하나가 성공했다고 전체가 정상인 것도 아니었다. Worker가 등록됐는지,
CNI가 올라왔는지, 시스템 Pod가 제대로 실행되는지 따로 확인해야 했다.

아직 Kubernetes를 능숙하게 다루는 단계는 아니다. 그래도 VM 3대를 직접
묶고 세 Node가 `Ready`가 되는 것까지 확인하면서, 적어도 각 프로그램이
왜 필요한지는 전보다 조금 알게 됐다.

다음 글에서는 이 클러스터에 Sock Shop을 올려볼 예정이다. 그 과정에서
Namespace, Deployment, ReplicaSet, Pod, Service가 어떻게 연결되는지와
실제로 발생한 오류를 정리해 보려고 한다.

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

