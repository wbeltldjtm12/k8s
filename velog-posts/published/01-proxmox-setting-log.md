# 데스크탑을 팔고 미니 PC에 Proxmox를 올렸다

데스크탑을 팔고 공부용 미니 PC를 장만했다.  
여기까지 오는 동안 참 많은 일이 있었지만, 우여곡절 끝에 AMD Ryzen 5 5500GT와 DDR4 RAM 8GB × 4로 구성된 미니 PC를 맞췄다.

막상 맞추고 나니 이걸 어디에 써야 할지 고민됐다.  
그러다가 여러 환경을 자유롭게 올리고 내릴 수 있으면 활용할 곳이 많을 것 같아 Proxmox를 설치해 보기로 했다.

마침 캡스톤 프로젝트에 사용할 서버도 필요했기 때문에, 가상화 환경을 직접 구성하면서 진행 과정을 기록해 보려고 한다.

# 1. Proxmox

사실 Proxmox가 뭔지도 잘 몰랐다.

찾아보니 컴퓨터 한 대를 여러 대처럼 나눠서 사용할 수 있게 해주는 가상화 플랫폼이라고 한다. 미니 PC에 Proxmox를 설치하고 그 위에 Ubuntu VM을 여러 개 만들어 사용할 수 있다.

내가 이해한 구조는 대충 이렇다.

```text
미니 PC
└─ Proxmox
   ├─ k8s-master
   ├─ k8s-worker1
   └─ k8s-worker2
```

이번에는 Ubuntu VM을 3개 만들고, 나중에 이 VM들로 Kubernetes를 구성해 볼 예정이다.

Kubernetes도 아직 제대로 아는 단계는 아니다. 현재 이해한 바로는 여러 서버에서 실행되는 컨테이너를 한곳에서 관리해 주는 도구다.

Proxmox가 미니 PC의 자원을 VM으로 나눠주는 역할이라면, Kubernetes는 그 VM들 위에서 컨테이너를 관리하는 역할 정도로 이해하고 있다.

일단 지금은 Kubernetes까지 설치한 것은 아니고, Ubuntu VM 3대를 만들어 둔 상태다.

## 1-1. 미니 PC 사양

현재 사용 중인 미니 PC의 사양은 다음과 같다.

> ### Mini PC Spec
>
> **CPU**　AMD Ryzen 5 5500GT · 6코어 12스레드  
> **RAM**　DDR4 8GB × 4 · 총 32GB  
> **SSD**　256GB × 2  
> **Hypervisor**　Proxmox VE 9.2.2

## 1-2. VM 자원 구성

32GB 메모리를 VM에 전부 할당하지 않고, Proxmox 호스트가 사용할 여유 자원으로 약 8GB를 남겼다.

나머지 자원은 VM 3대에 나눠서 할당했다.

| VM 이름 | VM ID | CPU | RAM | Disk |
| --- | ---: | ---: | ---: | ---: |
| `k8s-master` | 100 | 4코어 | 8GB | 60GB |
| `k8s-worker1` | 101 | 4코어 | 8GB | 60GB |
| `k8s-worker2` | 102 | 4코어 | 8GB | 60GB |

VM 이름은 나중에 헷갈리지 않도록 역할에 맞춰 미리 정했다.

아직 Kubernetes를 설치하거나 클러스터를 구성한 상태는 아니다. 현재는 Ubuntu VM 3대를 생성하고 이름과 자원만 지정해 둔 상태다.

## 1-3. Ubuntu VM 3대 생성

Proxmox GUI에서 Ubuntu 22.04 Live Server ISO를 사용해 VM 3대를 생성했다.

Ubuntu를 설치하면서 각 VM의 호스트 이름도 다음과 같이 설정했다.

- `k8s-master`
- `k8s-worker1`
- `k8s-worker2`

각 VM의 가상 디스크에는 **Discard(TRIM)** 옵션을 활성화했다.

> ### Discard(TRIM)이란?
>
> VM에서 파일을 삭제했을 때, 해당 디스크 공간을 더 이상 사용하지 않는다는 정보를 Proxmox와 SSD에 전달하는 기능이다.  
> 삭제된 공간을 스토리지에서 회수하고 SSD가 빈 공간을 효율적으로 관리하는 데 도움을 준다.
>
> 쉽게 말하면 **Ubuntu에서 비운 공간을 Proxmox와 SSD에도 알려주는 옵션**이다.

현재 VM 3대 모두 정상적으로 실행되고 있다.

![Proxmox에서 실행 중인 VM 3대](이미지-주소)

위 화면은 VM 102번인 `k8s-worker2`를 선택한 상태다.

- CPU: 4개
- RAM: 8GiB
- Bootdisk: 60GiB
- 상태: running

QEMU Guest Agent는 아직 설정하지 않았다.

## 1-4. `apt update` 401 Unauthorized 오류

Proxmox 설치를 마치고 패키지를 업데이트하려고 했는데 Enterprise 저장소에서 `401 Unauthorized` 오류가 발생했다.

Proxmox의 Enterprise 저장소는 유료 구독이 필요한 저장소다. 구독하지 않은 상태에서 해당 저장소에 접근해 오류가 발생한 것이었다.

`/etc/apt/sources.list.d/` 아래에 있는 Enterprise 관련 `.sources` 및 `.list` 파일을 제거하고, 무료로 사용할 수 있는 `pve-no-subscription` 저장소를 등록했다.

그런데 저장소를 변경한 뒤에도 업데이트가 바로 되지는 않았다. 현재 네트워크에서 IPv6 연결이 제대로 되지 않아 저장소 연결에 실패하고 있었다.

APT가 IPv4를 사용하도록 다음 옵션을 적용한 뒤 업데이트에 성공했다.

```text
Acquire::ForceIPv4=true
```

이 부분은 Proxmox 버전에 따라 저장소 주소와 설정 방식이 달라질 수 있으니, 다른 환경에서 그대로 따라 하기 전에는 설치된 버전을 먼저 확인해야 한다.

## 1-5. Tailscale 서브넷 라우터 구성

외부에서도 Proxmox와 내부 VM에 접속할 방법이 필요했다.

공유기에 포트포워딩을 설정하는 대신 Proxmox 호스트에 Tailscale을 설치하고, 내부 네트워크에 접근할 수 있도록 서브넷 라우터로 구성했다.

먼저 Proxmox 호스트에서 IPv4 포워딩을 활성화했다.

```text
net.ipv4.ip_forward = 1
```

그다음 Proxmox 호스트에서 공유기 내부 대역인 `192.168.0.0/24`를 Tailscale 서브넷 경로로 광고했다.

마지막으로 Tailscale Admin Console에 들어가 광고된 경로를 승인했다.

설정을 마친 뒤에는 외부 네트워크에서도 Tailscale에 연결하면 내부 IP를 사용해 다음 대상에 접근할 수 있게 되었다.

- Proxmox 관리 페이지
- `k8s-master`
- `k8s-worker1`
- `k8s-worker2`

공유기에 별도의 포트포워딩 규칙을 추가하지 않아도 된다는 점이 편했다.

## 1-6. 고정 IP는 설정하지 않았다

Ubuntu VM에는 공유기 기준의 고정 IP를 따로 설정하지 않았다.

각 VM에 Tailscale을 직접 설치한 뒤, VM별 Tailscale IP나 MagicDNS 이름을 이용해 접속할 예정이다.

현재는 Proxmox 호스트에만 Tailscale이 설치되어 있다. Proxmox가 `192.168.0.0/24` 대역으로 연결해 주는 서브넷 라우터 역할을 하고 있어 외부에서도 각 VM의 내부 IP로 접근할 수 있다.

## 현재까지 진행한 내용

- [x] Proxmox VE 9.2.2 설치
- [x] Ubuntu 22.04 VM 3대 생성
- [x] VM별 CPU, RAM, 디스크 할당
- [x] Discard(TRIM) 활성화
- [x] Proxmox 저장소 401 오류 해결
- [x] `pve-no-subscription` 저장소 등록
- [x] APT IPv4 강제 옵션 적용
- [x] Proxmox 호스트에 Tailscale 설치
- [x] Tailscale 서브넷 라우터 구성
- [x] 외부에서 내부망 접근 확인
- [ ] Ubuntu VM 3대에 Tailscale 설치
- [ ] 이후 서버 환경 구성

현재는 Proxmox 위에서 Ubuntu VM 3대가 정상적으로 실행되는 것까지 확인했다.

다음에는 각 VM에 Tailscale을 설치하고 노드끼리 정상적으로 통신하는지 확인해 볼 예정이다.

## 태그

`Proxmox` `미니PC` `홈서버` `Ubuntu` `Tailscale`상화`






