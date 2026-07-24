# kubeadm으로 3노드 Kubernetes 클러스터 구성하기

이전 글에서는 미니 PC에 Proxmox를 설치하고 Ubuntu VM 3대를 만들었다.

이번에는 이 VM들을 Kubernetes Control Plane 1대와 Worker Node 2대로
구성했다. 명령어를 전부 나열하기보다는, 설치하면서 새로 이해한 개념을
중심으로 정리한다.

> 환경: Ubuntu 22.04, Kubernetes v1.36.3, containerd, Calico v3.32.1

---

## 1. 구성한 클러스터

| 호스트 | IP | 역할 |
| --- | --- | --- |
| `k8s-master` | `192.168.0.12` | Control Plane |
| `k8s-worker1` | `192.168.0.13` | Worker |
| `k8s-worker2` | `192.168.0.14` | Worker |

Control Plane은 클러스터의 상태를 관리하고 Pod를 어느 노드에 배치할지
결정한다. Worker는 실제 애플리케이션 Pod가 실행되는 공간이다.

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

---

## 2. kubeadm, kubelet, kubectl의 차이

처음에는 이름이 비슷해서 세 프로그램의 역할이 가장 헷갈렸다.

- `kubeadm`: 클러스터를 처음 만들거나 노드를 참가시키는 도구
- `kubelet`: 각 노드에서 Pod 상태를 관리하는 서비스
- `kubectl`: 사용자가 Kubernetes API에 명령을 보내는 도구

즉, `kubeadm`으로 클러스터를 만들고, `kubectl`로 명령을 내리면,
각 노드의 `kubelet`이 실제 작업을 수행한다.

---

## 3. Docker 없이 containerd를 사용한 이유

Kubernetes가 컨테이너를 직접 실행하는 것은 아니다. kubelet은 CRI라는
표준 인터페이스를 통해 컨테이너 런타임에 실행을 요청한다.

```text
kubelet → CRI → containerd → runc → 컨테이너
```

따라서 Kubernetes 노드에 Docker Engine이 반드시 필요한 것은 아니다.
이번 클러스터에서는 containerd를 런타임으로 사용했다.

이 때문에 Docker로 받은 이미지와 Kubernetes가 받은 이미지가 서로 다르게
보일 수도 있다.

```bash
docker image ls
sudo crictl images
sudo ctr -n k8s.io images list
```

세 명령은 서로 다른 이미지 저장 공간을 확인한다.

---

## 4. 노드 기본 설정

설정은 master와 worker 세 노드에 모두 적용했다.

### swap 비활성화

```bash
sudo swapoff -a
```

재부팅 후에도 비활성화되도록 `/etc/fstab`의 swap 항목도 주석 처리했다.
kubelet이 노드의 메모리 상태를 일관되게 판단할 수 있도록 하기 위한
설정이다.

### 커널 모듈

```bash
sudo modprobe overlay
sudo modprobe br_netfilter
```

- `overlay`: 컨테이너 이미지의 레이어 파일 시스템에 사용
- `br_netfilter`: 브리지 네트워크 트래픽에 방화벽 규칙을 적용

### 네트워크 설정

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=1
```

Pod 트래픽이 노드와 네트워크 사이를 이동할 수 있도록 IP forwarding과
브리지 필터링을 활성화했다.

---

## 5. containerd와 Kubernetes 설치

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

```bash
sudo systemctl restart containerd
sudo systemctl enable containerd
```

이후 Kubernetes 공식 저장소를 등록하고 `kubelet`, `kubeadm`,
`kubectl`을 설치했다.

```bash
sudo apt install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

`apt-mark hold`는 일반 패키지 업데이트 중 Kubernetes 구성요소의 버전이
의도치 않게 바뀌는 것을 막는다.

> 저장소 등록 명령은 Kubernetes 버전에 따라 달라질 수 있으므로 실제
> 설치할 때는 공식 문서를 확인하는 것이 안전하다.

---

## 6. Control Plane 생성

`k8s-master`에서 다음과 같은 형태로 클러스터를 초기화했다.

```bash
sudo kubeadm init \
  --apiserver-advertise-address=192.168.0.12 \
  --pod-network-cidr=10.244.0.0/16
```

- `--apiserver-advertise-address`: Worker가 접속할 API Server 주소
- `--pod-network-cidr`: Pod에 할당할 가상 IP 범위

집 내부망인 `192.168.0.0/24`와 Pod 네트워크가 겹치지 않도록 서로 다른
대역을 사용했다.

초기화가 끝난 뒤 일반 사용자도 `kubectl`을 사용할 수 있도록 kubeconfig를
복사했다.

```bash
mkdir -p "$HOME/.kube"
sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
```

여기서 `sudo kubectl`을 습관적으로 사용하면 안 된다. 일반 사용자와 root는
서로 다른 kubeconfig를 읽기 때문에, root 설정이 없다면
`localhost:8080 connection refused` 오류가 발생할 수 있다.

---

## 7. Worker Node 연결

`kubeadm init` 결과로 출력된 join 명령을 두 Worker에서 실행했다.

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

---

## 8. Calico 설치

Worker가 클러스터에 참가해도 CNI가 없으면 노드는 `NotReady` 상태일 수
있다. Kubernetes는 Pod 배치를 관리하지만, Pod IP 할당과 노드 간 통신은
CNI 플러그인이 담당하기 때문이다.

이번에는 Calico를 사용했다.

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

상태는 다음 명령으로 확인했다.

```bash
kubectl get tigerastatus
kubectl get pods -n calico-system
kubectl get pods -n tigera-operator
```

---

## 9. 최종 확인

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

전체 시스템 Pod는 다음 명령으로 확인할 수 있다.

```bash
kubectl get pods -A
```

출력이 복잡해 보이지만 namespace별로 나누면 이해하기 쉽다.

- `kube-system`: Kubernetes 핵심 구성요소
- `calico-system`: Calico 네트워크 구성요소
- `tigera-operator`: Calico를 관리하는 Operator
- `default`: namespace를 지정하지 않은 일반 리소스

---

## 10. 이번에 이해한 전체 흐름

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

다음 글에서는 이 클러스터에 Sock Shop을 배포하면서 Namespace,
Deployment, Pod, Service와 장애 확인 방법을 정리할 예정이다.

---

## 복습할 질문

1. kubeadm, kubelet, kubectl은 각각 무엇을 하는가?
2. Kubernetes 노드에 Docker Engine이 없어도 되는 이유는 무엇인가?
3. containerd와 runc는 어떤 관계인가?
4. swap과 IP forwarding 설정은 왜 필요한가?
5. CNI가 없으면 노드가 왜 `NotReady`가 되는가?
6. Calico와 Tigera Operator는 어떤 관계인가?
7. 일반 `kubectl`과 `sudo kubectl`의 결과가 달라지는 이유는 무엇인가?

---

## 참고 문서

- [Installing kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/)
- [Creating a cluster with kubeadm](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/create-cluster-kubeadm/)
- [Calico Quickstart](https://docs.tigera.io/calico/latest/getting-started/kubernetes/quickstart)

## 게시 전 확인

- 실제 VM 사양
- 실제로 사용한 Pod CIDR과 Calico CIDR
- `kubectl get nodes` 스크린샷
- 토큰과 인증서 해시 노출 여부

