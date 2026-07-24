# 컨테이너와 Kubernetes 구성 중 알게 된 개념

Proxmox에 Ubuntu VM 3대를 만든 뒤 containerd와 Kubernetes를 구성하면서 처음 보는 용어가 계속 나왔다.

명령어만 그대로 실행하면 나중에 같은 작업을 할 때 다시 이해하지 못할 것 같아서, 궁금했던 내용을 하나씩 기록해 두기로 했다.

아직 모든 내용을 정확히 아는 단계는 아니며, 현재 설정을 진행하면서 이해한 내용을 기준으로 정리했다.

## QEMU Guest Agent란?

QEMU Guest Agent는 Proxmox 호스트와 Ubuntu VM 내부가 정보를 주고받을 수 있게 해주는 프로그램이다.

처음에는 VM이 정상적으로 실행되는데 왜 별도의 Agent가 필요한지 궁금했다.

Agent가 없어도 VM은 실행할 수 있지만, 설치하면 Proxmox에서 VM의 IP 주소를 확인하거나 VM을 안전하게 종료하는 등의 관리 작업이 편해진다. 백업할 때 VM 내부 파일시스템의 상태를 정리하는 데도 사용된다.

```text
Proxmox
   ↕ QEMU Guest Agent
Ubuntu VM
```

각 VM 내부에 Agent를 설치하고 Proxmox의 VM Options에서 QEMU Guest Agent 사용을 활성화해야 한다.

## LXC란?

LXC는 Proxmox 호스트의 Linux 커널을 공유하면서 독립된 Linux 환경을 실행하는 컨테이너 기술이다.

VM은 각각 별도의 운영체제와 커널을 실행하지만, LXC는 Proxmox의 커널을 공유한다.

```text
VM
└─ 각 VM이 별도의 운영체제와 커널 사용

LXC
└─ Proxmox의 Linux 커널을 공유
```

LXC는 VM보다 가볍고 빠르지만, 호스트 커널을 공유하므로 격리 수준과 구성 자유도는 VM보다 낮다.

간단한 웹 서버나 Registry 같은 서비스를 운영할 때 사용할 수 있다. 현재 만든 `k8s-master`, `k8s-worker1`, `k8s-worker2`는 LXC가 아니라 모두 Ubuntu VM이다.

## containerd란?

containerd는 컨테이너 이미지를 내려받고 실제 컨테이너를 생성하고 실행하는 컨테이너 런타임이다.

```text
Kubernetes
   ↓ 컨테이너 실행 요청
containerd
   ↓
컨테이너 생성 및 실행
```

containerd는 다음 작업을 담당한다.

- 컨테이너 이미지 다운로드
- 이미지 압축 해제 및 저장
- 컨테이너 생성
- 컨테이너 시작과 종료
- 실행 중인 컨테이너 상태 관리

처음에는 Kubernetes를 사용하려면 각 VM에 Docker를 설치해야 하는 줄 알았다.

하지만 Kubernetes는 containerd 같은 컨테이너 런타임과 직접 연결해 컨테이너를 실행할 수 있다.

현재 계획은 개발 PC의 Docker Desktop으로 이미지를 빌드하고 Registry에 올린 뒤, Kubernetes와 containerd가 해당 이미지를 내려받아 실행하는 방식이다.

```text
개발 PC
└─ Docker Desktop으로 이미지 빌드
        ↓ Push
Container Registry
        ↓ Pull
Ubuntu VM
└─ Kubernetes → containerd → 컨테이너 실행
```

## Container Registry란?

Container Registry는 빌드한 컨테이너 이미지를 저장하고 배포하는 저장소다.

Docker Hub나 GitHub Container Registry 같은 외부 서비스를 사용할 수 있고, 직접 사설 Registry를 만들 수도 있다.

```text
개발 PC
   ↓ docker push
Registry
   ↓ image pull
Kubernetes Worker
   ↓
Pod 실행
```

이미지 파일을 Worker VM마다 직접 복사하는 방법도 있지만, 이미지가 변경될 때마다 모든 노드에 다시 전달해야 하므로 번거롭다.

Registry를 사용하면 Kubernetes가 Pod를 실행할 Worker에 필요한 이미지를 자동으로 내려받을 수 있다.

추후 별도의 LXC나 VM에 사설 Registry를 구성하는 방법도 검토할 예정이다.

## `overlay` 커널 모듈

컨테이너 이미지는 여러 파일 계층으로 구성된다.

`overlay`는 여러 파일 계층을 하나의 파일시스템처럼 합쳐서 사용할 수 있게 해주는 Linux 커널 기능이다.

```text
기본 이미지
  + 설치한 패키지
  + 애플리케이션 파일
  + 컨테이너에서 변경한 파일
        ↓
하나의 파일시스템처럼 사용
```

containerd가 컨테이너 이미지와 파일시스템을 관리할 때 이 기능을 사용한다.

현재 부팅에서 모듈을 불러오는 명령은 다음과 같다.

```bash
sudo modprobe overlay
```

## `br_netfilter` 커널 모듈

컨테이너와 Pod는 Linux 브리지라는 가상 네트워크 장치를 통해 통신할 수 있다.

`br_netfilter`를 활성화하면 Linux 브리지를 통과하는 트래픽도 iptables가 확인하고 처리할 수 있다.

```text
Pod
 ↓
Linux Bridge
 ↓ br_netfilter
iptables 규칙
 ↓
다른 Pod 또는 노드
```

현재 부팅에서 모듈을 불러오는 명령은 다음과 같다.

```bash
sudo modprobe br_netfilter
```

## 커널 모듈 자동 로드

`modprobe`로 불러온 모듈은 현재 부팅에서만 활성화될 수 있다.

재부팅 후에도 `overlay`와 `br_netfilter`를 자동으로 불러오도록 다음 파일에 기록한다.

```bash
printf "overlay\nbr_netfilter\n" | sudo tee /etc/modules-load.d/k8s.conf
```

```text
/etc/modules-load.d/k8s.conf
├─ overlay
└─ br_netfilter
```

## IP forwarding이란?

IP forwarding은 Linux가 한 네트워크 인터페이스에서 받은 패킷을 다른 인터페이스로 전달할 수 있게 하는 기능이다.

```text
Pod 네트워크
  ↓
Ubuntu 노드
  ↓ IP forwarding
다른 Pod 또는 노드
```

활성화할 설정은 다음과 같다.

```text
net.ipv4.ip_forward = 1
```

Proxmox에서도 Tailscale 서브넷 라우터를 구성할 때 IP forwarding을 활성화했지만, 두 설정은 적용되는 위치와 목적이 다르다.

```text
Proxmox의 IP forwarding
└─ Tailscale 네트워크와 집 내부망 연결

Ubuntu VM의 IP forwarding
└─ Pod와 노드 네트워크 사이의 패킷 전달
```

## 컨테이너 네트워크용 sysctl 설정

각 VM에는 다음 설정을 적용한다.

```text
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
```

각 설정의 역할은 다음과 같다.

```text
bridge-nf-call-iptables
└─ 브리지를 통과하는 IPv4 패킷을 iptables가 처리

bridge-nf-call-ip6tables
└─ 브리지를 통과하는 IPv6 패킷을 ip6tables가 처리

ip_forward
└─ 노드가 패킷을 다른 네트워크로 전달
```

`sudo sysctl --system`을 실행하면 파일에 저장한 설정을 재부팅하지 않고 적용할 수 있다.

## `/etc/containerd` 디렉터리를 만드는 이유

containerd는 별도의 설정 파일이 없어도 내부 기본값으로 실행될 수 있다.

하지만 Kubernetes와 cgroup 관리 방식을 맞추려면 containerd의 일부 설정을 변경해야 한다.

containerd가 일반적으로 다음 파일에서 설정을 읽기 때문에 먼저 파일을 저장할 디렉터리를 만든다.

```text
/etc/containerd/config.toml
```

```bash
sudo mkdir -p /etc/containerd
```

이 명령은 containerd를 직접 설정하지 않는다. 설정 파일을 저장할 빈 디렉터리를 준비하는 명령이다.

```text
mkdir
└─ 설정 파일을 넣을 디렉터리 준비

containerd config default
└─ 기본 설정 내용 생성

sed
└─ 필요한 설정 변경

systemctl restart
└─ 변경한 설정을 containerd에 적용
```

## containerd 기본 설정 파일 생성

다음 명령은 설치된 containerd 버전에 맞는 기본 설정을 출력해 `/etc/containerd/config.toml` 파일로 저장한다.

```bash
containerd config default | sudo tee /etc/containerd/config.toml > /dev/null
```

명령의 흐름은 다음과 같다.

```text
containerd config default
└─ 기본 설정 내용을 출력
          ↓
         파이프
          ↓
sudo tee /etc/containerd/config.toml
└─ 전달받은 내용을 관리자 권한으로 파일에 저장
          ↓
       /dev/null
└─ 터미널에 다시 출력되는 내용은 버림
```

## cgroup이란?

cgroup은 Linux에서 프로세스별로 CPU와 메모리 같은 자원의 사용량을 나누고 제한하는 기능이다.

정식 이름은 Control Group이다.

예를 들어 Kubernetes에서 한 Pod의 메모리 한도를 1GB로 설정하면, 실제 Linux 수준에서 사용량을 제한하는 기능이 cgroup이다.

```text
Kubernetes
“이 Pod는 메모리를 1GB까지만 사용”
        ↓
containerd
“이 제한으로 컨테이너 실행”
        ↓
Linux cgroup
“실제 메모리 사용량 제한”
```

cgroup이 없다면 컨테이너 하나가 CPU와 메모리를 과도하게 사용해 다른 컨테이너나 서버 전체에 영향을 줄 수 있다.

## `SystemdCgroup = true` 설정

cgroup을 관리하는 방식에는 `cgroupfs`와 `systemd` 등이 있다.

Ubuntu는 시스템 서비스를 systemd로 관리한다. kubelet과 containerd도 cgroup을 systemd 방식으로 관리하도록 맞추기 위해 다음 값을 설정한다.

```toml
SystemdCgroup = true
```

```text
kubelet    → systemd 방식
containerd → systemd 방식
```

두 프로그램이 같은 방식으로 cgroup을 관리하게 맞추는 설정이라고 이해했다.

설정을 변경하는 명령은 다음과 같다.

```bash
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
```

변경 결과는 다음 명령으로 확인한다.

```bash
grep SystemdCgroup /etc/containerd/config.toml
```

정상 결과:

```text
SystemdCgroup = true
```

## CRI란?

CRI는 Kubernetes와 containerd 같은 컨테이너 런타임이 통신할 때 사용하는 공통 규칙이다.

정식 이름은 Container Runtime Interface다.

```text
Kubernetes
“컨테이너를 실행해 줘”
        ↓ CRI
containerd
“요청받은 컨테이너를 실행”
```

Kubernetes가 모든 컨테이너 런타임의 내부 구조를 각각 알아야 한다면 런타임이 바뀔 때마다 Kubernetes도 수정해야 한다.

그래서 런타임과 통신할 수 있는 공통 규칙인 CRI를 사용한다.

```text
                    ┌─ containerd
Kubernetes ── CRI ──┼─ CRI-O
                    └─ 다른 CRI 지원 런타임
```

현재 구성에서는 kubelet이 CRI를 통해 containerd에 컨테이너 실행을 요청한다.

```text
kubelet
   ↓ CRI
containerd
   ↓
runc
   ↓
컨테이너 프로세스 실행
```

containerd가 설치되어 있어도 CRI 기능이 비활성화되어 있으면 Kubernetes에서 사용할 수 없다.

## 다양한 컨테이너 런타임

컨테이너 런타임은 containerd 하나만 있는 것이 아니다.

대표적으로 containerd와 CRI-O가 있다.

```text
containerd
└─ 이미지 다운로드와 컨테이너 실행 및 상태 관리

CRI-O
└─ Kubernetes의 CRI 사용을 목적으로 만들어진 런타임
```

Docker Engine도 컨테이너를 실행하지만, 현재 Kubernetes는 Docker Engine과 직접 통신하지 않는다. Docker Engine을 Kubernetes 런타임으로 사용하려면 `cri-dockerd` 같은 중간 연결 프로그램이 필요하다.

현재 VM 3대에서는 여러 런타임 중 containerd를 사용한다.

## 현재까지 이해한 전체 흐름

```text
Kubernetes
   ↓
kubelet
   ↓ CRI
containerd
   ↓
runc
   ↓
Linux 커널
   ├─ cgroup으로 CPU와 메모리 관리
   ├─ overlay로 컨테이너 파일 계층 처리
   └─ br_netfilter와 IP forwarding으로 네트워크 처리
```

현재 단계는 Kubernetes를 설치하기 전에 각 VM에서 컨테이너 런타임과 Linux 커널 환경을 준비하는 과정이다.

## `kubectl get tigerastatus`란?

이 명령은 Tigera Operator로 설치한 Calico 네트워크 구성 요소들이 정상적으로 동작하는지 확인하는 명령이다.

```bash
kubectl get tigerastatus
```

`kubectl get`은 Kubernetes API에 특정 리소스의 현재 상태를 요청한다. `tigerastatus`는 Kubernetes에 기본으로 포함된 리소스가 아니라, Tigera Operator를 설치할 때 추가되는 사용자 정의 리소스다.

```text
kubectl
   ↓ Kubernetes API에 상태 요청
TigeraStatus
   ↓
Calico 구성 요소의 현재 상태 출력
```

Calico는 Pod 네트워크와 네트워크 정책을 담당하는 CNI 플러그인이다. Tigera Operator는 Calico의 설치와 상태 관리를 담당한다.

정상 출력은 다음과 비슷하다.

```text
NAME     AVAILABLE   PROGRESSING   DEGRADED   SINCE
calico   True        False         False      5m
```

각 항목은 다음 의미다.

```text
AVAILABLE=True
└─ 정상적으로 사용 가능한 상태

PROGRESSING=True
└─ 설치 또는 설정 변경이 아직 진행 중

DEGRADED=True
└─ 일부 구성 요소에 문제가 있는 상태
```

최종적으로 `AVAILABLE=True`, `PROGRESSING=False`, `DEGRADED=False`가 나오면 정상이다.

문제가 있을 때는 다음 명령으로 자세한 원인을 확인할 수 있다.

```bash
kubectl describe tigerastatus calico
```

설치된 구성에 따라 `calico` 외에도 `apiserver` 등의 항목이 표시될 수 있다.

다음 오류가 나온다면 TigeraStatus 리소스가 아직 생성되지 않은 상태다.

```text
error: the server doesn't have a resource type "tigerastatus"
```

Calico를 Tigera Operator 방식으로 설치하지 않았거나, Operator와 사용자 정의 리소스 설치가 아직 완료되지 않았는지 확인해야 한다.

## `free` 명령으로 메모리 확인하기

Ubuntu에서 현재 메모리 상태는 다음 명령으로 확인할 수 있다.

```bash
free
```

기본 출력은 KiB 단위라 바로 읽기 어려웠다. 다음처럼 `-h` 옵션을 사용하면 GiB와 MiB 단위로 볼 수 있다.

```bash
free -h
```

각 항목은 다음 의미다.

```text
total
└─ VM에 할당된 전체 메모리

used
└─ 실행 중인 프로그램이 주로 사용 중인 메모리

free
└─ 현재 아무 용도로도 사용하지 않는 메모리

buff/cache
└─ Linux가 성능을 높이기 위해 캐시로 사용하는 메모리

available
└─ 새 프로그램이 필요할 때 실제로 사용할 수 있는 메모리

Swap
└─ 디스크 일부를 메모리처럼 사용하는 공간
```

확인 당시 k8s-master의 결과는 대략 다음 상태였다.

```text
전체 RAM       약 7.75GiB
실제 사용 중   약 1.31GiB
완전히 빈 공간 약 2.57GiB
캐시           약 3.87GiB
사용 가능      약 6.15GiB
Swap           0
```

Linux는 남는 RAM을 비워두기보다 파일 캐시로 사용한다. 프로그램이 메모리를 더 요구하면 캐시를 줄여서 메모리를 넘겨주기 때문에 `free`보다 `available` 값을 보는 것이 중요하다.

Swap이 `0`으로 표시된 것은 Kubernetes 구성을 위해 swap을 비활성화한 상태이기 때문이다.

## Ubuntu와 Proxmox의 메모리 사용량이 다르게 보이는 이유

Ubuntu의 `free`에서는 사용량이 약 1.31GiB였지만, Proxmox Summary에서는 약 5.19GiB를 사용 중인 것으로 표시됐다.

처음에는 메모리를 과도하게 사용하는 것으로 보였지만, 두 화면이 메모리를 계산하는 기준이 달랐다.

```text
Ubuntu의 실제 사용량 약 1.31GiB
+ Linux buff/cache 약 3.87GiB
= Proxmox 표시 약 5.18GiB
```

Proxmox는 Linux가 캐시로 사용 중인 메모리까지 사용량에 포함해 보여준다.

```text
Ubuntu free의 used
└─ 실제 프로그램이 주로 사용 중인 메모리

Proxmox Memory usage
└─ 실제 프로그램 사용량과 Linux 캐시를 포함한 값
```

따라서 Proxmox의 사용률이 높아 보여도 Ubuntu의 `available` 메모리가 충분하다면 당장 메모리가 부족한 상태는 아니다.

Proxmox의 `Host memory usage`는 VM 내부 사용량보다 조금 더 높게 표시될 수 있다. 여기에는 VM을 실행하는 QEMU 프로세스와 가상화에 필요한 추가 메모리도 포함되기 때문이다.

캐시는 필요할 때 Linux가 자동으로 회수하므로, 화면의 사용률을 낮추기 위해 강제로 캐시를 비울 필요는 없다.

## VM 디스크 크기 기록 확인

Proxmox Summary 화면에서 k8s-master의 Bootdisk가 `32.00GiB`로 표시됐다.

기존 Velog 초안에는 모든 VM의 디스크가 60GB로 기록되어 있으므로 실제 설정을 다시 확인할 필요가 있다.

```text
k8s-master
└─ Proxmox 화면상 Bootdisk 32GiB

k8s-worker1, k8s-worker2
└─ 실제 Proxmox Hardware 또는 Summary에서 별도 확인 필요
```

블로그에는 처음 계획한 값이 아니라 실제 Proxmox 화면에서 확인한 값을 기록해야 한다.
