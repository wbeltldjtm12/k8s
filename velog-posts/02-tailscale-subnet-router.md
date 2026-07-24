# VM마다 Tailscale을 설치하지 않고 외부에서 접속하기

Proxmox 위에 Ubuntu VM 3대를 만들고 나니 한 가지 고민이 생겼다.

> 외부에서 각 VM에 접속하려면 모든 VM에 Tailscale을 설치해야 할까?

결론부터 말하면 현재 구성에서는 그럴 필요가 없었다. Proxmox 호스트 한 대에만 Tailscale을 설치하고 **서브넷 라우터(Subnet Router)**로 사용하면 내부 VM까지 접근할 수 있다.

## 현재 네트워크 구성

현재 미니 PC는 벽 랜선에 연결된 ipTIME 공유기 아래에 있다.

ipTIME이 사용하는 주소는 다음과 같았다.

```text
외부 IP: 192.168.100.8
내부 IP: 192.168.0.1
내부 대역: 192.168.0.0/24
```

외부 IP로 표시된 `192.168.100.8`도 실제 공인 IP가 아닌 건물 내부망의 사설 IP다. 즉, 현재는 건물 네트워크와 ipTIME을 거치는 이중 NAT 구조다.

하지만 Tailscale은 내부 장치에서 외부로 연결을 시작하기 때문에, 이중 NAT 환경에서도 별도의 포트포워딩 없이 사용할 수 있었다.

전체 구조는 다음과 같다.

```text
건물 네트워크
    ↓ 벽 랜선
ipTIME 공유기
    ↓
Proxmox 호스트
    ↓
Ubuntu VM 3대
```

## VM의 DHCP 주소 예약

VM의 내부 IP가 바뀌면 외부에서 접속할 때마다 새 주소를 찾아야 한다.

Ubuntu 안에서 고정 IP를 직접 설정하는 대신 ipTIME의 **DHCP 주소 예약** 기능을 사용했다.

| VM | 예약 IP |
| --- | --- |
| `k8s-master` | `192.168.0.12` |
| `k8s-worker1` | `192.168.0.13` |
| `k8s-worker2` | `192.168.0.14` |

각 VM은 계속 DHCP로 주소를 자동 할당받는다. 다만 ipTIME이 VM의 MAC 주소를 확인해 항상 같은 IP를 배정한다.

따라서 Ubuntu에서 다음 명령을 실행했을 때 IP 옆에 `dynamic`이 표시되어도 정상이다.

```bash
ip -4 addr
```

Ubuntu 입장에서는 여전히 DHCP를 사용하지만, 공유기에서 같은 주소를 계속 배정해 주기 때문이다.

## 서브넷 라우터란?

서브넷 라우터를 간단히 표현하면 다음과 같다.

> 외부의 Tailscale 네트워크와 집 내부 네트워크를 연결해 주는 출입문

현재는 Proxmox 호스트에만 Tailscale이 설치되어 있다. Proxmox가 `192.168.0.0/24` 대역의 서브넷 라우터 역할을 한다.

```text
외부 노트북
    ↓ Tailscale
Proxmox 호스트
    ↓ ipTIME 내부 네트워크
    ├─ 192.168.0.12  k8s-master
    ├─ 192.168.0.13  k8s-worker1
    └─ 192.168.0.14  k8s-worker2
```

외부 노트북에서 `192.168.0.12`로 접속하면 Tailscale이 해당 트래픽을 Proxmox로 보낸다. Proxmox는 전달받은 트래픽을 내부 네트워크의 `k8s-master`로 다시 전달한다.

VM은 Tailscale에 직접 가입되어 있지 않아도 된다.

## 경로 광고는 무슨 뜻일까?

Tailscale을 설정하면서 `192.168.0.0/24` 대역을 **광고한다**는 표현이 등장했다.

여기서 광고는 일반적인 홍보가 아니라 다음 의미다.

> `192.168.0.x` 주소로 가려면 이 Proxmox 호스트를 통과하면 된다.

Proxmox가 이 경로를 Tailscale에 알리고, Tailscale Admin Console에서 해당 경로를 승인하면 같은 Tailnet의 장치들이 Proxmox를 통해 내부망으로 접근할 수 있다.

`192.168.0.0/24`는 현재 환경에서 대략 `192.168.0.1`부터 `192.168.0.254`까지의 내부 주소 대역을 뜻한다.

## 외부에서 VM으로 접속하는 방법

외부 노트북이 같은 Tailnet에 연결되어 있다면 다음과 같이 VM에 접속할 수 있다.

```bash
ssh <사용자명>@192.168.0.12
ssh <사용자명>@192.168.0.13
ssh <사용자명>@192.168.0.14
```

이 방식이 동작하려면 다음 조건이 필요하다.

- Proxmox 호스트가 실행 중이어야 한다.
- Proxmox에서 Tailscale 서비스가 실행 중이어야 한다.
- Proxmox에서 IP 포워딩이 활성화되어 있어야 한다.
- `192.168.0.0/24` 경로가 Tailscale Admin Console에서 승인되어 있어야 한다.
- 외부 장치가 같은 Tailnet에 연결되어 있어야 한다.

Proxmox가 내부망으로 들어가는 출입문이므로 Proxmox가 꺼지면 외부에서 VM으로 접근할 수 없다. 다만 같은 내부망에 있는 VM끼리의 통신에는 영향을 주지 않는다.

## 실제 설정 및 확인 명령

서브넷 라우터는 다른 네트워크의 패킷을 VM이 있는 내부망으로 전달해야 한다. 먼저 Proxmox에서 IPv4 포워딩을 활성화했다.

```bash
sudo sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl --system
```

설정값은 `sysctl net.ipv4.ip_forward`로 확인하며, 정상 결과는 `net.ipv4.ip_forward = 1`이다.

이후 Proxmox가 내부망으로 가는 길을 제공한다고 Tailscale에 알렸다.

```bash
sudo tailscale set --advertise-routes=192.168.0.0/24
tailscale debug prefs | grep -A3 AdvertiseRoutes
```

실제 출력에서 `AdvertiseRoutes`에 `192.168.0.0/24`가 들어 있는 것을 확인했다.

여기서 중요한 점은 **경로 광고와 경로 승인은 서로 다른 작업**이라는 것이다.

> 광고는 Proxmox가 “이 내부망으로 가는 길을 제공할 수 있다”고 알리는 것이고, 승인은 Tailscale 관리자가 그 길의 사용을 허용하는 것이다.

Tailscale Admin Console의 Machines 페이지에서 `ch`를 선택하고, `Edit route settings`에서 `192.168.0.0/24`를 활성화한 뒤 저장해야 한다.

연결 상태와 Proxmox의 Tailscale IP는 다음 명령으로 확인한다.

```bash
tailscale status
tailscale ip -4
```

Proxmox 장치 `ch`와 Windows 장치 `node`는 온라인이었다. 과거 master VM에 직접 설치했던 Tailscale 장치는 오프라인이지만, VM마다 Tailscale을 설치하지 않는 현재 구조에서는 다시 켤 필요가 없다.

## Windows에서 서브넷 경로 사용하기

Windows가 서브넷 경로를 수락하도록 관리자 PowerShell에서 설정했다.

```powershell
tailscale set --accept-routes=true
```

VM의 SSH 포트와 실제 접속은 다음과 같이 확인한다.

```powershell
Test-NetConnection 192.168.0.12 -Port 22
ssh master@192.168.0.12
```

`TcpTestSucceeded : True`라면 VM의 22번 포트까지 도달한 것이다.

## 같은 Wi-Fi에서 테스트하며 헷갈린 점

처음에는 Windows PC와 VM이 모두 ipTIME의 `192.168.0.0/24` 내부망에 있는 상태로 테스트했다.

```text
InterfaceAlias   : Wi-Fi
SourceAddress    : 192.168.0.8
TcpTestSucceeded : True
```

이 결과는 VM의 SSH 포트가 정상이라는 뜻이지만, Tailscale 서브넷 라우터를 거쳤다는 증거는 아니다. 같은 공유기 아래에 있으므로 내부 Wi-Fi로 직접 연결됐을 가능성이 있기 때문이다.

진짜 외부 접속을 확인하려면 Windows PC를 휴대폰 핫스팟처럼 다른 대역의 네트워크에 연결한다. `ipconfig`에서 Wi-Fi 주소가 기존 `192.168.0.x`가 아닌지 확인한 뒤 테스트한다.

```powershell
tailscale ping ch
Test-NetConnection 192.168.0.12 -Port 22
ssh master@192.168.0.12
```

| 명령 | 확인하는 구간 |
| --- | --- |
| `tailscale ping ch` | Windows와 Proxmox 사이의 Tailscale 연결 |
| `Test-NetConnection ... -Port 22` | 외부망에서 VM의 SSH 포트까지의 연결 |
| `ssh master@...` | 실제 SSH 로그인 |

## 접속이 안 될 때 확인할 순서

`Connection timed out`이 발생하면 다음 순서로 구간을 나누어 확인한다.

1. `tailscale status`로 Proxmox가 온라인인지 확인한다.
2. `tailscale debug prefs`로 `192.168.0.0/24`가 광고 중인지 확인한다.
3. `sysctl net.ipv4.ip_forward`의 결과가 `1`인지 확인한다.
4. Proxmox에서 `ping -c 3 192.168.0.12`가 성공하는지 확인한다.
5. Admin Console에서 `192.168.0.0/24` 경로가 승인됐는지 확인한다.
6. Windows에서 `tailscale set --accept-routes=true`를 적용한다.
7. `Test-NetConnection`의 `SourceAddress`로 현재 연결된 네트워크를 확인한다.

> 작성 시점에는 내부망에서 VM의 SSH 포트가 열려 있는 것까지 확인했다. 외부망 접속은 Admin Console의 경로 승인 상태를 다시 확인하며 최종 검증 중이다.
## VM마다 Tailscale을 설치하지 않은 이유

현재 목적은 외부에서 Proxmox와 VM에 안전하게 접속하는 것이다. 이 정도는 Proxmox 한 대를 서브넷 라우터로 사용하는 것만으로 충분하다.

VM마다 Tailscale을 설치하지 않으면 다음과 같은 장점이 있다.

- VM마다 Tailscale을 설치하고 업데이트할 필요가 없다.
- Tailnet에 등록되는 장치 수가 줄어든다.
- VM끼리는 기존 내부 LAN을 그대로 사용한다.
- Tailscale 설정과 VM 내부 환경을 분리할 수 있다.
- VM을 새로 만들거나 삭제할 때 Tailscale 장치를 따로 정리할 필요가 없다.

물론 VM마다 Tailscale을 설치하는 방법도 있다. 다음과 같은 기능이 필요해지면 그때 검토할 수 있다.

- 각 VM에 개별 Tailscale IP를 부여하고 싶은 경우
- MagicDNS 이름으로 각 VM에 직접 접속하고 싶은 경우
- Tailscale ACL로 VM별 접근 권한을 따로 관리하고 싶은 경우
- VM을 다른 내부망이나 다른 장소로 옮길 가능성이 있는 경우

현재는 이런 기능이 꼭 필요하지 않아 Proxmox에만 Tailscale을 설치하는 단순한 구성을 유지하기로 했다.

## 내부 IP를 블로그에 공개해도 될까?

이 글에 사용한 다음 주소는 모두 **사설 IP 주소**다.

```text
192.168.0.12
192.168.0.13
192.168.0.14
```

`192.168.x.x` 대역은 내부 네트워크에서 사용하도록 예약된 주소다. 인터넷에서 이 주소만 입력해 내 VM에 직접 접속할 수는 없다. 다른 집이나 회사에서도 똑같은 주소를 사용할 수 있다.

따라서 VM의 사설 IP를 블로그에 적는 것은 일반적으로 문제가 없다.

다만 스크린샷을 올릴 때는 다음 정보가 노출되지 않았는지 확인하는 것이 좋다.

- 장비의 MAC 주소
- Tailscale 인증 키
- SSH 개인 키
- 로그인 계정의 비밀번호
- 실제 공인 IP 주소
- 공유기 관리자 비밀번호
- 도메인 및 인증서의 비밀키

MAC 주소만으로 인터넷에서 장비에 접속할 수 있는 것은 아니다. 하지만 특정 네트워크 장비를 식별할 수 있는 값이므로 블로그 스크린샷에서는 가리는 편이 좋다.

## 정리

현재 구성은 다음과 같이 정리할 수 있다.

- Tailscale은 Proxmox 호스트에만 설치한다.
- Proxmox를 `192.168.0.0/24` 대역의 서브넷 라우터로 사용한다.
- Ubuntu VM에는 Tailscale을 설치하지 않는다.
- VM의 주소는 ipTIME DHCP 주소 예약으로 유지한다.
- VM끼리의 통신은 내부 LAN을 사용한다.
- 외부 접속만 Tailscale과 Proxmox를 거친다.
- 경로 광고와 Admin Console의 경로 승인은 별도 작업이다.
- 같은 Wi-Fi에서 성공한 접속은 외부 접속 검증이 아니다.

최종 목표는 Proxmox 한 대가 외부에서 내부 서버로 들어오는 출입문 역할을 하도록 만드는 것이다. 외부망 최종 검증이 끝나면 결과도 이어서 기록할 예정이다.

## 태그

`Proxmox` `Tailscale` `SubnetRouter` `ipTIME` `홈서버` `미니PC` `Ubuntu` `네트워크`
