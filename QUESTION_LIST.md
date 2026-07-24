# KUBEIN 질문 목록

마지막 갱신: 2026-07-25

이 파일은 KUBEIN 연구, 홈서버, Docker, Kubernetes 실습 중 AI에게 물어본
질문을 한곳에 모으기 위한 기록이다.

## 기록 규칙

- 앞으로 사용자가 질문 형태로 남긴 내용은 가능한 한 원래 의미를 유지해 추가한다.
- 같은 주제라도 궁금한 지점이 다르면 별도 질문으로 기록한다.
- 비밀번호, API 키, 토큰, 인증서 hash 같은 민감정보는 기록하지 않는다.
- 단순 감탄이나 작업과 관계없는 대화는 제외한다.
- 답을 들었다고 질문을 삭제하지 않는다. 나중에 직접 다시 답해 보는 것이 목적이다.
- 블로그 글에 사용한 질문은 관련 초안 경로를 함께 표시할 수 있다.

상태 표기:

- `✅`: 답변을 듣고 문서에도 정리함
- `🧪`: 답변은 들었지만 실제 환경에서 다시 확인해야 함
- `⏳`: 아직 결론을 내리지 못했거나 후속 실험이 필요함

---

## 2026-07-25 신규 질문

- ✅ patch는 무엇이며 내가 신경 써야 하는 파일인가?
- ✅ 현재 MD 파일에 내가 실제로 한 질문 목록이 보이는가?
- ✅ CRI 등 원본 기록에 있던 질문이 현재 글에서 빠진 것은 아닌가?
- ✅ 공식 구조도와 결과 스크린샷은 글의 어느 위치에 넣어야 하는가?
- ✅ 앞으로 질문을 숨김파일이 아닌 별도 목록에 계속 기록할 수 있는가?
- ✅ 벨로그 관련 파일이 너무 많이 쌓였을 때 게시본, 초안, 원본을 어떻게 정리해야 하는가?
- ✅ 두 번째로 게시할 Kubernetes 벨로그 글을 질문 중심의 게시용 흐름으로 어떻게 다시 정리할 것인가?
- ✅ 실제로 진행한 작업을 중심에 두면서 당시 궁금했던 내용을 각 단계에 어떻게 녹일 것인가?
- ✅ containerd, CRI, cgroup, CNI처럼 처음 등장하는 용어를 글 안에서 어떻게 설명할 것인가?

---

## A. KUBEIN 연구와 논문

- ⏳ KUBEIN 저장소의 전체 구조는 어떻게 되어 있는가?
- ⏳ 현재 설계나 설명에 서로 모순되는 부분이 있는가?
- ⏳ 현재 결과가 논문으로서 가치가 있는가?
- ⏳ 기존 KubeIn 논문을 확장해 후속 논문으로 낼 가치가 있는가?
- ⏳ 이미 비슷한 논문을 썼는데 같은 시스템으로 다시 논문을 내도 되는가?
- ⏳ 후속 논문이 기존 논문과 다른 새로운 연구 사이클이 되려면 무엇이 필요한가?
- ⏳ KUBEIN을 다시 논문화하려면 어떤 기능과 실험을 보완해야 하는가?
- ⏳ RCA 연구에서는 결국 정답률이 가장 중요한 지표인가?
- ⏳ RCA 기반 LLM 설명을 Kubernetes 밖으로 일반화한다는 것은 정확히 무엇인가?
- ⏳ 일반화된 RCA 설명기가 있으면 무엇이 좋아지고, 없으면 어떤 문제가 생기는가?
- ⏳ 일반 LLM에 장애 정보를 주는 것과 RCA 기반 설명 시스템은 무엇이 다른가?
- ⏳ 이 연구가 단순한 프롬프트 엔지니어링과 다른 점은 무엇인가?
- ⏳ 이 주제가 소프트웨어 엔지니어 진로와 맞는가?
- ⏳ Kubernetes를 계속 사용하지 않고도 연구 주제를 확장할 수 있는가?
- ⏳ KCI 수준의 후속 주제로 어떤 실험을 설계할 수 있는가?
- ⏳ 관련 연구 12편은 어떤 기준으로 선정해야 하는가?
- ⏳ 배포가 성공한 것과 RCA 알고리즘이 검증된 것은 왜 다른가?
- ⏳ 장애 탐지, 근본 원인 추론, 설명 품질을 어떤 지표로 분리해 평가해야 하는가?
- ⏳ 같은 입력에서 비교 방법을 공정하게 실행하려면 무엇을 통제해야 하는가?
- ⏳ 반복 실험의 재현성을 어떻게 확보해야 하는가?

---

## B. Proxmox와 VM

- ✅ QEMU Guest Agent는 무엇이며 VM이 이미 실행되는데 왜 필요한가?
- ✅ LXC는 VM과 무엇이 다른가?
- ✅ Kubernetes 노드를 LXC가 아니라 Ubuntu VM으로 만든 이유는 무엇인가?
- ✅ Proxmox와 Ubuntu VM의 메모리 사용량이 서로 다르게 보이는 이유는 무엇인가?
- ✅ `free -h`의 `free`, `available`, buff/cache는 각각 무엇인가?
- ✅ VM에 디스크를 크게 할당했는데 Ubuntu 루트 파일시스템은 왜 작게 보이는가?
- ✅ 가상 디스크, 파티션, LVM PV·VG·LV, 파일시스템은 어떤 관계인가?
- ✅ `df`, `lsblk`, `vgs`, `lvs`는 각각 무엇을 확인하는 명령인가?
- ✅ Proxmox에서 설정한 디스크 용량과 게스트에서 인식한 디스크 용량이 다를 수 있는가?
- ✅ Tailscale subnet router를 사용하면 VM마다 Tailscale을 설치하지 않아도 되는가?

---

## C. 컨테이너와 Docker

- ✅ Dockerfile로 이미지를 만든다는 것은 무엇인가?
- ✅ Dockerfile의 각 명령은 어떤 이미지 레이어를 만드는가?
- ✅ `COPY requirements.txt`를 소스 코드보다 먼저 실행하는 이유는 무엇인가?
- ✅ Docker 이미지를 만들면 requirements 파일의 의존성도 함께 설치되는가?
- ✅ `docker build`와 `docker push`는 각각 무엇을 하는가?
- ✅ 이미지를 빌드하면 Docker Hub로 바로 올라가는가, 로컬에만 생성되는가?
- ✅ `tag does not exist` 오류는 왜 발생했는가?
- ✅ Docker 이미지가 700~800MB로 커진 이유는 무엇인가?
- ✅ Docker Hub의 compressed size와 로컬 disk usage가 다른 이유는 무엇인가?
- ✅ Docker Hub 저장소가 Public이면 어떤 정보와 레이어가 공개되는가?
- ✅ Public 이미지 안에 API 키나 `.env`를 넣으면 왜 위험한가?
- ✅ `docker pull`한 이미지는 현재 디렉터리가 아니라 서버 어디에 저장되는가?
- ✅ `docker image ls`와 `docker images ls`는 왜 다르게 동작하는가?
- ✅ Docker Engine과 Kubernetes의 containerd는 같은 이미지 저장소를 사용하는가?
- ✅ `docker image ls`, `crictl images`, `ctr -n k8s.io images list`는 무엇이 다른가?
- ✅ master에서 이미지를 pull하면 worker에도 자동으로 복사되는가?
- ✅ Kubernetes는 어떤 노드에서 이미지를 pull할지 어떻게 결정하는가?
- ✅ `imagePullPolicy`의 `Always`, `IfNotPresent`, `Never`는 어떻게 다른가?
- ✅ Docker socket 권한 오류는 왜 발생하는가?
- ✅ 사용자를 `docker` 그룹에 추가한 뒤 재로그인이 필요한 이유는 무엇인가?
- ✅ Snap Docker에서 `docker.service`를 찾지 못한 이유는 무엇인가?
- ✅ Snap 방식 Docker와 APT 방식 Docker는 서비스와 저장 경로가 어떻게 다른가?

---

## D. Kubernetes 구성과 동작

- ✅ kubeadm, kubelet, kubectl은 각각 무엇을 하는가?
- ✅ Control Plane과 Worker Node의 역할은 어떻게 다른가?
- ✅ Kubernetes를 사용하려면 Docker Engine을 반드시 설치해야 하는가?
- ✅ containerd는 정확히 무엇을 담당하는가?
- ✅ containerd와 runc는 어떻게 다른가?
- ✅ CRI(Container Runtime Interface)는 무엇인가?
- ✅ kubelet과 containerd 사이에서 CRI는 어떤 역할을 하는가?
- ✅ containerd 외에는 어떤 컨테이너 런타임이 있는가?
- ✅ `/etc/containerd` 디렉터리와 `config.toml`을 직접 만드는 이유는 무엇인가?
- ✅ containerd의 CRI plugin이 비활성화되면 어떤 문제가 생기는가?
- ✅ cgroup은 무엇이며 Pod의 CPU·메모리 제한과 어떤 관계가 있는가?
- ✅ `SystemdCgroup = true`는 왜 설정하는가?
- ✅ kubelet과 containerd의 cgroup driver가 다르면 어떤 문제가 생기는가?
- ✅ swap을 왜 비활성화했는가?
- ✅ 최신 Kubernetes에서 swap을 사용하도록 별도로 설정할 수도 있는가?
- ✅ `overlay` 커널 모듈은 어디에 사용되는가?
- ✅ `br_netfilter`는 왜 필요한가?
- ✅ 커널 모듈을 재부팅 후에도 자동으로 불러오려면 어떻게 해야 하는가?
- ✅ IP forwarding은 왜 활성화해야 하는가?
- ✅ Proxmox와 Kubernetes 노드의 IP forwarding은 목적이 어떻게 다른가?
- ✅ `net.bridge.bridge-nf-call-iptables`는 무엇을 바꾸는가?
- 🧪 `kubeadm init`에 실제로 사용한 Pod CIDR은 무엇이었는가?
- ✅ Pod CIDR과 집 LAN 대역이 겹치면 왜 문제가 되는가?
- ✅ `kubeadm init`은 내부적으로 어떤 구성요소를 만드는가?
- ✅ `/etc/kubernetes/manifests`의 static Pod manifest는 누가 실행하는가?
- ✅ kubeconfig는 무엇이며 왜 일반 사용자 홈으로 복사하는가?
- ✅ `sudo kubectl`은 왜 `localhost:8080`으로 접속하려 했는가?
- ✅ bootstrap token과 CA certificate hash는 각각 어떤 역할을 하는가?
- ✅ Worker의 join 명령을 잃어버리거나 토큰이 만료되면 어떻게 하는가?
- ✅ Worker가 join됐는데도 처음에 `NotReady`일 수 있는 이유는 무엇인가?
- ✅ CNI는 무엇이며 왜 Kubernetes와 별도로 설치해야 하는가?
- ✅ Calico는 어떤 역할을 하는가?
- ✅ Tigera Operator와 Calico는 어떤 관계인가?
- ✅ `kubectl get tigerastatus`는 무엇을 확인하는 명령인가?
- ✅ `AVAILABLE`, `PROGRESSING`, `DEGRADED`는 각각 무엇을 의미하는가?
- ✅ namespace를 지정하지 않으면 `kubectl`은 어디를 조회하는가?
- ✅ namespace를 매번 입력하지 않고 기본값으로 설정할 수 있는가?

---

## E. GitHub Actions와 CI/CD

- ✅ GitHub에 push하면 이미지를 자동으로 빌드하고 서버에 배포하는 방식을 무엇이라 하는가?
- ✅ GitHub Actions Secret에는 어떤 값을 넣어야 하는가?
- ✅ GitHub Actions에서 Docker Hub 토큰은 어떻게 참조하는가?
- ✅ 지금까지 사용한 GitHub Actions와 Docker Hub 기능은 무료인가?
- ✅ GitHub-hosted runner와 self-hosted runner는 무엇이 다른가?
- ✅ self-hosted runner를 k8s-master에 설치하면 어떤 권한을 갖게 되는가?
- ✅ runner를 systemd 서비스로 등록하는 이유는 무엇인가?
- ✅ Actions 화면에 `Run workflow` 버튼이 나타나지 않는 이유는 무엇인가?
- ✅ workflow 파일은 저장소의 어느 경로에 있어야 하는가?
- ✅ `workflow_dispatch`는 무엇을 활성화하는가?
- ✅ CI/CD가 성공하면 KUBEIN 엔진 검증도 끝난 것인가?
- ✅ 애플리케이션 배포 workflow와 Prometheus 설치 workflow를 분리한 이유는 무엇인가?
- ✅ 자동화하기 전에 수동 실행을 먼저 성공시켜야 하는 이유는 무엇인가?
- ✅ CI/CD 로그의 connect timeout과 전체 timeout은 어떻게 다른가?
- ⏳ 현재 KUBEIN에서 CI/CD가 연구 핵심인가, 편의 기능인가?

---

## F. Prometheus와 LLM 설정

- ✅ Prometheus는 KUBEIN에서 왜 필요한가?
- ✅ Prometheus Pod가 `Running`인데 NodePort로 접속하지 못할 수 있는 이유는 무엇인가?
- ✅ Prometheus 설치는 성공했는데 GitHub Actions가 실패한 이유는 무엇인가?
- ✅ NodePort, Service, Endpoint, Pod를 어떤 순서로 확인해야 하는가?
- ✅ Prometheus를 GitHub Actions가 아니라 서버에서 직접 설치해도 되는가?
- ✅ `PROMETHEUS_URL`은 어디에 설정해야 하는가?
- ✅ Gemini 2.5 Flash API 설정은 Dockerfile이 아니라 어디에 두어야 하는가?
- ✅ `.env.cluster`를 Git 저장소에 올리면 안 되는 이유는 무엇인가?
- ⏳ Gemini 무료 티어의 제한이 반복 실험에 어떤 영향을 주는가?

---

## G. Namespace, YAML, Sock Shop 장애 분석

- ✅ Docker Compose로 실행한 KUBEIN과 Kubernetes Pod는 어떻게 다른가?
- ✅ `python main.py` 직접 실행과 FastAPI 서버 실행은 무엇이 다른가?
- ✅ 엔진 검증용 직접 실행 CLI가 필요한 이유는 무엇인가?
- ✅ Kubernetes namespace는 Linux 디렉터리와 같은 개념인가?
- ✅ 현재 디렉터리가 `sock-shop`이면 kubectl namespace도 자동으로 바뀌는가?
- ✅ `kubectl describe`에서 리소스 종류와 namespace를 모두 지정해야 하는 이유는 무엇인가?
- ✅ YAML에 리소스 종류를 명령으로 쓰지 않아도 Kubernetes가 알아보는 이유는 무엇인가?
- ✅ `apiVersion`, `kind`, `metadata`, `spec`은 각각 무엇인가?
- ✅ YAML 한 파일로 여러 리소스가 생성되는 이유는 무엇인가?
- ✅ Deployment, ReplicaSet, Pod는 어떤 순서로 연결되는가?
- ✅ Pod의 `Evicted` 상태는 무엇을 의미하는가?
- ✅ `ephemeral-storage`에는 어떤 데이터가 포함되는가?
- ✅ 디스크 부족인데 왜 메모리 명령인 `free -h`가 아니라 `df -h`를 봐야 하는가?
- ✅ Whisker가 적은 용량만 사용했는데도 Evicted된 이유는 무엇인가?
- ✅ `BestEffort` QoS Pod가 먼저 퇴거될 수 있는 이유는 무엇인가?
- ✅ Events와 애플리케이션 로그는 각각 무엇을 보여주는가?
- ✅ `kubectl logs --previous`가 로그를 가져오지 못할 수 있는 이유는 무엇인가?
- ✅ `CrashLoopBackOff`는 근본 원인인가, 반복 재시작 상태인가?
- ✅ MongoDB 5 이상이 AVX 없는 CPU에서 실행되지 않은 이유는 무엇인가?
- ✅ `mongo`처럼 버전을 생략한 이미지가 재현성을 깨뜨리는 이유는 무엇인가?
- ✅ MongoDB 이미지를 `4.4`로 고정한 이유는 무엇인가?
- ✅ 오래된 `beta.kubernetes.io/os` nodeSelector 경고는 무엇을 의미하는가?
- ✅ 실패한 Calico Pod를 삭제하기 전에 정상 Replica 수를 확인해야 하는 이유는 무엇인가?

---

## H. 블로그 작성과 학습

- ✅ 구축 과정을 어떤 순서로 글로 나누는 것이 좋은가?
- ✅ 설치 명령만 나열하지 않고 공부 기록으로 만들려면 무엇을 설명해야 하는가?
- ✅ AI의 도움을 받았다는 사실을 글에 어떻게 자연스럽게 밝힐 수 있는가?
- ✅ 실제로 AI에게 했던 질문을 글에 어느 정도까지 넣는 것이 좋은가?
- ✅ 공식 Kubernetes와 Calico 구조도는 어느 위치에 넣어야 하는가?
- ✅ 직접 실행한 명령 결과 중 어떤 스크린샷을 증거로 넣는 것이 좋은가?
- ✅ 출처 캡션은 어떤 형식으로 작성해야 하는가?
- ✅ 글이 너무 길거나 너무 짧지 않게 어느 정도 분량으로 조절해야 하는가?
- ✅ 기술적으로 맞더라도 AI 티가 나는 문장을 기존 글의 내 말투로 어떻게 고칠 것인가?
- ✅ 벨로그 작성을 제외하면 실제 인프라 구축과 KUBEIN 연구 준비는 어디까지 진행됐는가?

---

## 다음 질문 추가 위치

새 질문은 우선 이 아래에 날짜와 함께 추가한 뒤, 관련 주제가 분명해지면 위의
분류로 옮긴다.

