# 미니 PC 홈서버 구축기 #1 — Proxmox 설치부터 Kubernetes VM 준비까지

> 이 글은 미니 PC에 Proxmox를 설치하고 Kubernetes 클러스터용 가상머신을 준비한 과정을 기록한 글이다.  
> 현재는 Kubernetes를 설치하기 전 단계이며, Proxmox와 원격 접속 환경까지만 구성했다.

## 1. 시작하며

개인 프로젝트인 `kubein`을 진행하기 위해 미니 PC에 홈서버 환경을 구축하기 시작했다.

최종 목표는 Proxmox 위에 Ubuntu 가상머신 3대를 만들고, 해당 VM으로 Kubernetes 클러스터를 구성하는 것이다. 같은 작업을 다시 하거나 비슷한 오류를 만났을 때 참고할 수 있도록 설치 과정과 시행착오를 하나씩 기록하기로 했다.

## 2. 서버 사양

사용한 미니 PC의 사양은 다음과 같다.

| 구분 | 사양 |
| --- | --- |
| CPU | AMD Ryzen 5 5500GT |
| 메모리 | DDR4 32GB |
| 스토리지 | SSD 512GB × 2 |
| 하이퍼바이저 | Proxmox VE 9.2.2 |

## 3. VM 자원 배분 설계

32GB 메모리를 Kubernetes 노드에 모두 할당하지 않고, Proxmox 호스트 운영과 여유 자원을 위해 약 8GB를 남겼다.

| 역할 | VM ID | CPU | RAM | Disk |
| --- | ---: | ---: | ---: | ---: |
| Control Plane 예정 | 100 | 4코어 | 8GB | 60GB |
| Worker 1 예정 | 101 | 4코어 | 8GB | 60GB |
| Worker 2 예정 | 102 | 4코어 | 8GB | 60GB |
| Proxmox 호스트 | - | 공유 | 약 8GB 확보 | 별도 사용 |

VM 이름은 역할을 쉽게 구분할 수 있도록 다음과 같이 정했다.

- `k8s-master`
- `k8s-worker1`
- `k8s-worker2`

`k8s-master`라는 이름을 사용했지만, 아직 Kubernetes Control Plane을 구성한 상태는 아니다. 현재는 Ubuntu VM의 이름만 미리 지정한 것이다.

## 4. Ubuntu VM 3대 생성

Proxmox GUI에서 Ubuntu 22.04 Live Server ISO를 사용해 VM 3대를 생성했다.

Ubuntu 설치 과정에서 각 VM의 호스트 이름도 VM 이름과 동일하게 설정했다.

SSD를 사용하는 환경이므로 각 가상 디스크의 **Discard(TRIM)** 옵션을 활성화했다. 게스트 운영체제에서 사용하지 않는 블록 정보를 스토리지에 전달할 수 있도록 하기 위한 설정이다.

현재 Proxmox 화면에서 다음 VM이 모두 실행되는 것을 확인했다.

- `100 (k8s-master)`
- `101 (k8s-worker1)`
- `102 (k8s-worker2)`

화면에서 확인한 `k8s-worker2`의 상태는 다음과 같다.

- 상태: `running`
- CPU: 4 CPUs
- 메모리: 8.00GiB
- 부트 디스크: 60.00GiB
- HA: 사용하지 않음
- QEMU Guest Agent: 아직 설정하지 않음

> 스크린샷 삽입 위치: Proxmox VE 9.2.2에서 VM 100~102가 실행 중인 화면

## 5. Proxmox 저장소 401 오류 해결

Proxmox VE 9는 Debian Trixie 기반이다. 초기 설정 후 `apt update`를 실행했을 때 Enterprise 저장소에서 다음과 같은 문제가 발생했다.

```text
401 Unauthorized
```

구독이 없는 환경에서 유료 Enterprise 저장소에 접근하면서 발생한 오류였다.

해결을 위해 `/etc/apt/sources.list.d/` 아래에 있던 Enterprise 관련 `.sources` 및 `.list` 파일을 제거하고, 무료 커뮤니티 저장소인 `pve-no-subscription`을 등록했다.

또한 현재 네트워크에서 IPv6 연결이 원활하지 않아 APT가 저장소에 연결하지 못하는 문제가 있었다. 다음 옵션으로 IPv4 사용을 강제한 뒤 패키지 목록 업데이트에 성공했다.

```text
Acquire::ForceIPv4=true
```

실제로 적용한 저장소 주소와 명령어는 추후 설정 파일을 다시 확인한 뒤 별도로 보완할 예정이다. Proxmox 버전에 따라 저장소 경로가 달라질 수 있으므로 다른 버전에 명령어를 그대로 적용해서는 안 된다.

## 6. Tailscale 서브넷 라우터 구축

집 밖에서도 포트포워딩 없이 Proxmox와 내부 VM에 접근할 수 있도록 Proxmox 호스트에 Tailscale을 설치했다.

먼저 Linux의 IPv4 포워딩을 활성화했다.

```text
net.ipv4.ip_forward = 1
```

이후 Proxmox 호스트가 공유기 내부망을 Tailscale 네트워크에 광고하도록 `192.168.0.0/24` 대역을 서브넷 경로로 지정했다.

마지막으로 Tailscale Admin Console에서 광고된 경로를 승인했다.

구성 결과, 외부 네트워크에서도 Tailscale에 접속하면 다음 대상에 내부 IP로 접근할 수 있게 되었다.

- Proxmox 관리 화면
- `k8s-master`
- `k8s-worker1`
- `k8s-worker2`

공유기에 별도의 포트포워딩 규칙을 만들지 않아도 된다는 점이 가장 큰 장점이다.

## 7. 고정 IP를 설정하지 않은 이유

Ubuntu VM에는 공유기 기준의 고정 IP를 따로 설정하지 않았다. 앞으로 각 VM에 Tailscale을 직접 설치하고, VM별 Tailscale IP 또는 MagicDNS 이름을 관리 주소로 사용할 계획이다.

현재 단계에서는 Proxmox 호스트만 Tailscale에 연결되어 있으며, Proxmox가 `192.168.0.0/24` 대역의 서브넷 라우터 역할을 한다.

따라서 현재 상태와 향후 계획을 구분하면 다음과 같다.

| 구분 | 현재 | 예정 |
| --- | --- | --- |
| Proxmox 호스트 | Tailscale 설치 및 서브넷 라우터 구성 완료 | 유지 |
| Ubuntu VM 3대 | 내부 DHCP 주소 사용 | Tailscale 직접 설치 |
| 외부 접속 | Proxmox를 경유해 내부 IP로 접근 | VM별 Tailscale IP 또는 MagicDNS 사용 |
| Kubernetes | 미설치 | Tailscale 구성 확인 후 설치 |

## 8. 현재까지 완료한 작업

- [x] 미니 PC 하드웨어 사양 확인
- [x] Proxmox VE 설치
- [x] Kubernetes용 VM 자원 배분 설계
- [x] Ubuntu 22.04 VM 3대 생성
- [x] VM별 호스트 이름 지정
- [x] 가상 디스크 Discard(TRIM) 활성화
- [x] Proxmox Enterprise 저장소의 401 오류 해결
- [x] `pve-no-subscription` 저장소 등록
- [x] APT에서 IPv4 연결 강제
- [x] Proxmox 호스트에 Tailscale 설치
- [x] IPv4 포워딩 활성화
- [x] `192.168.0.0/24` 서브넷 경로 광고 및 승인
- [x] 외부에서 내부망 접근 환경 구성
- [ ] 각 Ubuntu VM에 Tailscale 설치
- [ ] VM 간 Tailscale 통신 확인
- [ ] containerd 설치
- [ ] `kubeadm`, `kubelet`, `kubectl` 설치
- [ ] Kubernetes Control Plane 구성
- [ ] Worker 노드 연결
- [ ] CNI 설치

## 9. 현재 상태

현재 Proxmox 위에서 Ubuntu VM 3대가 정상적으로 실행되고 있다.

아직 Kubernetes와 관련 구성 요소는 설치하지 않았다. 즉, 현재 단계는 **Kubernetes 클러스터 구축을 위한 가상화 환경과 원격 접근 기반을 준비한 상태**라고 정리할 수 있다.

다음 작업에서는 각 Ubuntu VM에 Tailscale을 직접 설치하고 VM 간 통신을 확인할 예정이다.

## 태그

`Proxmox` `홈서버` `미니PC` `Ubuntu` `Kubernetes` `Tailscale` `DevOps`
