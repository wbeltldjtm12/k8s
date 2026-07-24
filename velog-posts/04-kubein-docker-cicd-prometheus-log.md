# KUBEIN 배포 삽질 기록: Docker 이미지부터 GitHub Actions와 Prometheus까지

> 작성일: 2026-07-24  
> 목적: KUBEIN을 서버에 배포하면서 궁금했던 내용과 실제로 발생한 오류를 다시 설명할 수 있도록 정리한 학습 기록이다.  
> 주의: 토큰, 비밀번호, API 키는 절대로 글이나 저장소에 기록하지 않는다.

## 1. 이번에 만들려고 한 것

로컬 PC에서 KUBEIN 코드를 수정한 뒤 매번 직접 이미지를 빌드하고 서버로 옮기는 과정이 번거로웠다.
그래서 다음과 같은 흐름을 만들기로 했다.

```text
로컬 PC
  └─ GitHub main 브랜치에 push
       └─ GitHub Actions
            ├─ backend 이미지 빌드
            ├─ frontend 이미지 빌드
            └─ Docker Hub에 push
                 └─ k8s-master의 self-hosted runner
                      ├─ 새 이미지 pull
                      └─ Docker Compose로 KUBEIN 재배포
```

Kubernetes 클러스터는 다음 3개 노드로 구성되어 있다.

| 노드 | IP | 역할 |
| --- | --- | --- |
| `k8s-master` | `192.168.0.12` | Control Plane, KUBEIN 앱 실행 |
| `k8s-worker1` | `192.168.0.13` | Worker |
| `k8s-worker2` | `192.168.0.14` | Worker |

Prometheus는 애플리케이션 CI/CD에 포함하지 않고 Kubernetes master에서 Helm으로 직접 관리하기로 했다.

---

## 2. Dockerfile로 이미지를 만든다는 것은 무엇인가?

Dockerfile은 애플리케이션을 실행할 환경과 순서를 적어 둔 빌드 설명서다.

Python 백엔드라면 일반적으로 다음과 같은 흐름을 가진다.

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

여기서 중요한 점은 `requirements.txt`가 이미지 안에 단순히 복사만 되는 것이 아니라,
`RUN pip install -r requirements.txt` 단계에서 패키지까지 이미지 내부에 설치된다는 것이다.

즉, 다음 두 줄은 역할이 다르다.

```dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```

- `COPY`: 파일을 이미지 빌드 환경 안으로 가져온다.
- `RUN`: 이미지를 만드는 도중 명령을 실행해 패키지를 설치한다.

Dockerfile 외에 Prometheus 설치 같은 서버 인프라 설정까지 `requirements.txt`에 넣을 수는 없다.
`requirements.txt`는 Python 패키지 목록이며, Kubernetes용 Prometheus는 Helm chart나 Kubernetes manifest로 관리해야 한다.

### 기억할 질문

- Dockerfile의 각 명령이 어떤 레이어를 만드는가?
- `COPY requirements.txt`를 먼저 하는 이유는 무엇인가?
- 소스 코드만 바뀌었을 때 의존성 설치 레이어를 재사용할 수 있는가?

---

## 3. `docker build`와 `docker push`의 차이

이미지를 빌드하면 우선 현재 Docker Engine의 로컬 이미지 저장소에 생성된다.
로그인된 Docker Hub 계정으로 자동 업로드되는 것은 아니다.

```bash
docker build -t sch02/kubein-backend:v1 ./backend
docker build -t sch02/kubein-frontend:v1 ./frontend
```

빌드 결과 확인:

```bash
docker image ls
```

그다음 별도로 push해야 Docker Hub에 올라간다.

```bash
docker push sch02/kubein-backend:v1
docker push sch02/kubein-frontend:v1
```

처음에 다음 오류가 발생했다.

```text
tag does not exist: sch02/kubein-backend:v1
```

원인은 Docker Hub 문제가 아니라, 로컬에 해당 이름과 태그를 가진 이미지가 아직 없었기 때문이다.

확인할 때는 저장소 이름뿐 아니라 태그까지 정확히 봐야 한다.

```bash
docker image ls sch02/kubein-backend
```

### 핵심 정리

```text
docker build
  └─ 로컬 Docker에 이미지 생성

docker push
  └─ 로컬 이미지를 Registry에 업로드

docker pull
  └─ Registry의 이미지를 현재 서버로 다운로드
```

---

## 4. Docker 이미지가 큰 이유

처음 생성한 이미지 크기는 대략 다음과 같았다.

- backend: 약 843MB
- frontend: 약 770MB

이미지에는 애플리케이션 소스만 들어가는 것이 아니다.

- 기본 Linux 파일 시스템
- Python 런타임
- 시스템 라이브러리
- Python 패키지
- 애플리케이션 소스
- 빌드 과정에서 남은 캐시나 불필요한 파일

Docker Hub 화면의 `compressed size`와 로컬 `docker image ls`의 크기도 다를 수 있다.
Registry로 전송할 때 레이어가 압축되기 때문이다.

이미지를 줄이는 대표적인 방법은 다음과 같다.

- `python:3.11-slim`과 같은 작은 베이스 이미지 사용
- `.dockerignore`에 `.git`, 가상환경, 캐시, 데이터 파일 추가
- `pip install --no-cache-dir` 사용
- 빌드 도구가 필요한 경우 multi-stage build 사용
- 불필요한 패키지를 `requirements.txt`에서 제거

단, 이미지 크기를 줄인다고 무조건 Alpine을 선택하면 안 된다.
일부 Python 패키지는 Alpine의 musl 환경에서 빌드가 복잡하거나 이미지 빌드 시간이 더 길어질 수 있다.

---

## 5. Docker Hub 이미지가 Public이면 무엇이 보이는가?

Docker Hub 저장소가 Public이면 누구나 다음 작업을 할 수 있다.

```bash
docker pull sch02/kubein-backend:v1
```

이미지 레이어와 이미지에 포함된 파일도 분석할 수 있다고 생각해야 한다.
따라서 이미지에 다음 항목을 포함하면 안 된다.

- Gemini API 키
- Docker Hub 토큰
- `.env` 파일
- SSH 개인 키
- 비밀번호
- Kubernetes 인증 파일

환경변수 이름이나 기본 설정은 이미지에 들어갈 수 있지만, 실제 비밀값은 서버의 `.env.cluster` 또는 GitHub Actions Secret으로 주입해야 한다.

Docker Hub 저장소를 Private으로 변경하면 pull할 때 인증이 필요하다.
Kubernetes에서 Private 이미지를 사용한다면 `imagePullSecret`도 설정해야 한다.

---

## 6. pull한 Docker 이미지는 서버의 어디에 저장되는가?

이미지를 `docker pull`로 받았다고 해서 하나의 `.tar` 파일이 작업 디렉터리에 생기는 것은 아니다.
Docker Engine이 자신의 데이터 디렉터리에 이미지 레이어와 메타데이터 형태로 관리한다.

일반적인 Docker Engine의 저장 위치:

```text
/var/lib/docker
```

일반적인 containerd의 저장 위치:

```text
/var/lib/containerd
```

그러나 내부 파일을 직접 찾아서 수정하거나 삭제하면 안 된다.
항상 런타임 명령을 사용해야 한다.

Docker 이미지 확인:

```bash
docker image ls
```

Docker가 실제로 사용하는 데이터 경로 확인:

```bash
docker info --format '{{.DockerRootDir}}'
```

Kubernetes의 containerd 이미지 확인:

```bash
sudo crictl images
```

또는 Kubernetes namespace를 지정해 containerd를 직접 확인할 수 있다.

```bash
sudo ctr -n k8s.io images list
```

### `docker images ls`가 이상했던 이유

다음 명령은 `ls`를 이미지 이름 필터처럼 해석할 수 있다.

```bash
docker images ls
```

정확한 명령은 다음과 같다.

```bash
docker image ls
```

`docker images`도 이미지 목록을 표시하는 별칭이지만, 뒤에 `ls`를 추가하지 않는다.

---

## 7. Docker와 containerd는 같은 이미지 저장소를 사용하는가?

같은 서버에 Docker와 containerd가 모두 설치되어 있어도 일반적으로 서로 다른 이미지 저장소를 사용한다.

```text
docker pull
  └─ Docker Engine의 이미지 저장소
       └─ /var/lib/docker

Kubernetes가 이미지 pull
  └─ containerd의 k8s.io namespace
       └─ /var/lib/containerd
```

따라서 `docker image ls`에 이미지가 있다고 해서 Kubernetes의 containerd에도 반드시 존재하는 것은 아니다.
반대도 마찬가지다.

KUBEIN 앱은 현재 master에서 Docker Compose로 실행하므로 Docker Engine의 이미지를 사용한다.
Kubernetes Pod로 배포한 워크로드는 containerd가 이미지를 관리한다.

---

## 8. master에서 이미지를 받으면 worker에도 자동으로 들어가는가?

단순히 master에서 `docker pull`을 실행한다고 worker 노드에 이미지가 복사되지는 않는다.

Kubernetes에서는 다음 흐름으로 동작한다.

```text
Deployment 또는 Pod 생성
  └─ Scheduler가 실행 노드 결정
       └─ 해당 노드의 containerd가 Registry에서 이미지 pull
            └─ Pod 실행
```

즉, Pod가 `worker1`에 배치되면 `worker1`의 containerd가 이미지를 받는다.
Pod가 `worker2`에 배치되면 `worker2`가 받는다.

관련 설정:

```yaml
imagePullPolicy: IfNotPresent
```

- `IfNotPresent`: 노드에 이미지가 없을 때만 pull
- `Always`: Pod 시작 시 Registry에서 이미지 정보를 확인
- `Never`: 로컬에 있는 이미지만 사용

같은 `latest` 태그를 계속 쓰면 어떤 코드가 배포됐는지 추적하기 어렵다.
그래서 CI/CD에서는 Git commit SHA를 이미지 태그로 사용하는 편이 안전하다.

---

## 9. Docker 권한 오류는 왜 발생했는가?

일반 사용자로 다음 명령을 실행했을 때 권한 오류가 발생했다.

```bash
docker ps
```

```text
permission denied while trying to connect to the Docker API
at unix:///var/run/docker.sock
```

Docker CLI는 Docker daemon의 Unix socket에 접근해야 한다.
일반 사용자가 해당 socket에 접근할 권한이 없어서 발생한 오류다.

일반적인 해결 방법:

```bash
sudo usermod -aG docker "$USER"
```

그룹을 추가한 직후 기존 로그인 세션에는 반영되지 않을 수 있다.
로그아웃 후 다시 로그인하거나 다음 명령으로 새 그룹 세션을 시작한다.

```bash
newgrp docker
```

그다음 확인:

```bash
id
docker ps
```

주의할 점은 `docker` 그룹 사용자가 사실상 root 수준의 작업을 수행할 수 있다는 것이다.
아무 사용자나 docker 그룹에 추가하면 안 된다.

---

## 10. Snap으로 설치한 Docker와 APT Docker의 차이

처음에는 Docker를 Snap으로 설치했기 때문에 다음 명령이 실패했다.

```bash
sudo systemctl restart docker
```

```text
Unit docker.service not found.
```

Snap 패키지는 일반 APT 패키지와 서비스 이름이나 파일 배치가 다를 수 있다.
프로젝트 문서와 일반적인 Ubuntu 서버 운영 방식을 맞추기 위해 Snap 버전을 제거하고 APT 패키지로 다시 설치했다.

설치 전 시뮬레이션:

```bash
sudo apt install --simulate docker.io docker-compose-v2
```

실제 설치:

```bash
sudo apt update
sudo apt install docker.io docker-compose-v2
```

서비스 확인:

```bash
sudo systemctl enable --now docker
sudo systemctl status docker
```

Snap 설치를 계속 사용해도 무조건 잘못된 것은 아니다.
다만 서비스 관리 방식과 경로가 달라질 수 있으므로 하나의 설치 방식으로 통일하는 것이 문제 해결에 유리하다.

---

## 11. CI/CD란 무엇이며 왜 도입했는가?

원했던 기능은 다음과 같았다.

> GitHub에 코드를 push하면 자동으로 이미지를 만들고 서버에 배포하고 싶다.

이 과정을 CI/CD라고 부른다.

- CI(Continuous Integration): 코드를 합칠 때 빌드와 테스트를 자동 실행
- CD(Continuous Delivery/Deployment): 검증된 결과물을 배포 가능한 상태로 만들거나 실제 환경에 자동 배포

이번 프로젝트의 흐름은 다음과 같다.

```text
git push
  └─ GitHub-hosted runner
       ├─ backend/frontend 이미지 빌드
       └─ Docker Hub push
            └─ self-hosted runner(k8s-master)
                 ├─ 새 이미지 pull
                 ├─ Docker Compose 실행
                 └─ health check
```

이 방식을 사용하면 로컬에서 매번 다음 작업을 반복할 필요가 없다.

- Docker 이미지 수동 빌드
- 이미지 태그 입력
- Docker Hub push
- 서버 SSH 접속
- 새 이미지 pull
- 컨테이너 재시작

---

## 12. GitHub Actions Secret에는 무엇을 넣었는가?

Docker Hub에 이미지를 push하려면 인증 정보가 필요하다.
비밀번호나 토큰을 workflow YAML에 직접 쓰면 안 된다.

GitHub 저장소의 다음 위치에 Repository Secret을 추가했다.

```text
Settings
  └─ Secrets and variables
       └─ Actions
            └─ New repository secret
```

등록한 Secret 이름:

```text
DOCKERHUB_TOKEN
```

workflow에서는 실제 토큰 문자열 대신 다음과 같이 참조한다.

```yaml
password: ${{ secrets.DOCKERHUB_TOKEN }}
```

Secret은 로그에 그대로 출력하지 않고, 최소 권한을 가진 access token을 사용하는 것이 좋다.

---

## 13. GitHub-hosted runner와 self-hosted runner의 차이

GitHub-hosted runner는 GitHub가 제공하는 일회성 빌드 머신이다.
Docker 이미지를 빌드하고 Registry에 push하는 작업에 적합하다.

self-hosted runner는 내가 관리하는 서버에 설치한 Actions 실행기다.
이번에는 `k8s-master`에 설치했다.

```text
GitHub-hosted runner
  └─ 외부 빌드 담당

self-hosted runner
  └─ 내부 서버 배포 담당
```

self-hosted runner가 정상 등록되면 다음과 같은 메시지를 확인할 수 있다.

```text
Connected to GitHub
Runner successfully added
Settings Saved
```

서버에서는 다음 항목을 확인했다.

```bash
docker ps
kubectl get nodes
test -f /home/master/kubein-config/.env.cluster
```

self-hosted runner는 저장소의 workflow 명령을 실제 서버에서 실행한다.
따라서 신뢰할 수 없는 Pull Request가 무조건 runner에서 실행되지 않도록 trigger와 권한을 주의해야 한다.

---

## 14. `Run workflow` 버튼이 보이지 않는 이유

GitHub Actions 화면에서 수동 실행 버튼을 표시하려면 workflow에 `workflow_dispatch` trigger가 있어야 한다.

```yaml
on:
  workflow_dispatch:
```

또한 workflow 파일이 기본 브랜치에 존재해야 하고 GitHub가 YAML을 정상적으로 인식해야 한다.

확인할 항목:

- `.github/workflows/*.yml` 경로가 맞는가?
- workflow 파일이 GitHub에 push됐는가?
- YAML 문법 오류가 없는가?
- 기본 브랜치가 올바른가?
- `workflow_dispatch`가 있는가?

---

## 15. Prometheus는 왜 필요한가?

KUBEIN이 Kubernetes 장애 원인을 분석하려면 현재 클러스터의 상태와 시계열 지표가 필요하다.
Prometheus는 CPU, 메모리, Pod, 노드 등의 지표를 수집하고 PromQL로 조회할 수 있게 해준다.

이번에는 `kube-prometheus-stack` Helm chart를 사용했다.

설치 결과 다음 구성요소가 실행됐다.

- Prometheus Operator
- Prometheus Server
- kube-state-metrics
- 각 노드의 node-exporter

확인 명령:

```bash
kubectl get pods -n monitoring -o wide
kubectl get services -n monitoring
```

Prometheus 서비스는 다음 NodePort를 사용한다.

```text
http://192.168.0.12:30090
```

준비 상태 확인:

```bash
curl --connect-timeout 5 --max-time 10 \
  -fsS http://192.168.0.12:30090/-/ready
```

---

## 16. Prometheus 설치는 성공했는데 Actions가 실패한 이유

GitHub Actions 로그에서는 Prometheus 관련 Pod가 모두 `Running`이었다.
그런데 마지막 curl 검증이 반복적으로 timeout되어 workflow가 실패했다.

원인은 Prometheus 설치 실패가 아니라 잘못된 IP였다.

```text
잘못 사용한 예전 IP: 192.168.67.13
현재 master IP:     192.168.0.12
```

즉, 다음 두 상황을 구분해야 한다.

```text
Pod가 Running이 아님
  └─ 설치 또는 Kubernetes 내부 문제

Pod와 Service는 정상인데 외부 curl 실패
  └─ IP, 포트, 방화벽, NodePort, 라우팅 문제 가능성
```

처음 curl에는 짧은 timeout이 지정되지 않아 한 번의 연결 시도가 약 2분 이상 대기했다.
재시도까지 합쳐 Actions가 약 29분 동안 멈춘 것처럼 보였다.

그래서 health check에 다음 옵션을 추가했다.

```bash
--connect-timeout 5 --max-time 10
```

- `--connect-timeout 5`: 연결 수립을 최대 5초만 기다린다.
- `--max-time 10`: 요청 전체를 최대 10초로 제한한다.

자동화에서는 실패 자체뿐 아니라 **얼마나 빨리 실패를 감지할지**도 중요하다.

---

## 17. Prometheus를 CI/CD에서 분리한 이유

애플리케이션은 코드를 자주 수정하고 배포한다.
반면 Prometheus는 클러스터 인프라이므로 매 애플리케이션 배포마다 다시 설치하거나 업그레이드할 필요가 없다.

그래서 역할을 다음과 같이 분리했다.

```text
애플리케이션 CI/CD
  ├─ KUBEIN backend
  └─ KUBEIN frontend

클러스터에서 수동 관리
  └─ kube-prometheus-stack
```

수동 설치 또는 업그레이드 명령:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts \
  --force-update

helm repo update

helm upgrade --install monitoring \
  prometheus-community/kube-prometheus-stack \
  --version 86.0.0 \
  --namespace monitoring \
  --create-namespace \
  --values cluster-port/monitoring-values.yaml \
  --atomic \
  --timeout 10m
```

현재는 설치가 이미 진행된 상태이므로, 무조건 다시 설치하기 전에 먼저 다음을 확인한다.

```bash
helm list -n monitoring
kubectl get pods -n monitoring
```

---

## 18. Gemini 2.5 Flash 설정은 어디에 두는가?

KUBEIN의 LLM 설정은 서버의 환경변수 파일에서 관리한다.

```dotenv
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=실제_API_KEY
PROMETHEUS_URL=http://192.168.0.12:30090
```

실제 파일 위치:

```text
/home/master/kubein-config/.env.cluster
```

이 파일은 Git에 commit하지 않는다.
저장소에는 실제 키가 없는 `.env.cluster.example`만 둔다.

환경변수를 바꾼 뒤에는 실행 중인 컨테이너를 재생성해야 새 값이 반영된다.

---

## 19. 현재까지 완료된 것

- Kubernetes 3개 노드가 모두 `Ready`
- backend와 frontend Docker 이미지 빌드
- Docker Hub push
- GitHub 원격 저장소 연결
- Docker Hub token을 GitHub Actions Secret으로 등록
- `k8s-master`에 self-hosted runner 등록
- 앱 빌드 및 배포 workflow 작성
- Prometheus stack 설치
- 실제 클러스터 IP를 `192.168.0.12~14`로 문서와 설정에 반영
- Prometheus 자동 설치 workflow 제거
- Prometheus 수동 관리 문서 추가
- 긴 health check 대기를 막는 curl timeout 추가

현재 로컬 Git 상태:

```text
main 브랜치가 origin/main보다 1커밋 앞선 상태
커밋: 256d1a0 Manage Prometheus manually on cluster
```

---

## 20. 바로 다음에 할 일

### 1단계: Prometheus 실제 접근 확인

```bash
curl --connect-timeout 5 --max-time 10 \
  -fsS http://192.168.0.12:30090/-/ready
```

### 2단계: 서버 환경변수 수정

```bash
sed -i \
  's#^PROMETHEUS_URL=.*#PROMETHEUS_URL=http://192.168.0.12:30090#' \
  /home/master/kubein-config/.env.cluster

grep '^PROMETHEUS_URL=' /home/master/kubein-config/.env.cluster
```

### 3단계: 로컬 커밋 push

```bash
git push origin main
```

### 4단계: GitHub Actions 확인

- backend 이미지 빌드 성공 여부
- frontend 이미지 빌드 성공 여부
- Docker Hub push 성공 여부
- self-hosted runner 배포 성공 여부
- health check 성공 여부

### 5단계: 서비스 확인

```bash
curl -fsS http://192.168.0.12:8000/api/health
curl -fsS http://192.168.0.12:8501/_stcore/health
curl -fsS http://192.168.0.12:30090/-/ready
```

브라우저에서 확인:

```text
Frontend:   http://192.168.0.12:8501
Backend:    http://192.168.0.12:8000
Prometheus: http://192.168.0.12:30090
```

---

## 21. 이번 작업에서 얻은 교훈

### 1. `Running`과 외부 접근 가능은 다르다

Pod가 `Running`이라고 해서 NodePort 주소까지 반드시 접근 가능한 것은 아니다.
Pod, Service, Endpoint, IP와 방화벽을 단계별로 확인해야 한다.

### 2. Docker와 Kubernetes의 이미지 저장소는 구분해야 한다

Docker Compose는 Docker Engine을 사용하고 Kubernetes는 containerd를 사용한다.
같은 서버여도 이미지 목록이 다를 수 있다.

### 3. 자동화하기 전에 수동 절차를 이해해야 한다

수동으로 한 번도 성공하지 않은 작업을 바로 자동화하면 어느 단계가 실패했는지 파악하기 어렵다.
설치, 검증, 복구 절차를 먼저 확인하고 반복 작업을 자동화하는 편이 좋다.

### 4. 애플리케이션과 인프라의 변경 주기는 다르다

KUBEIN 앱은 자주 배포할 수 있지만 Prometheus는 상대적으로 드물게 변경한다.
모든 것을 하나의 workflow에 넣는 것보다 변경 주기와 책임에 따라 분리하는 것이 관리하기 쉽다.

### 5. IP와 포트는 문서 한 곳만 고치면 끝나지 않는다

기본값, `.env` 예제, Compose, workflow, README에 예전 IP가 각각 남을 수 있다.
전체 저장소 검색으로 확인해야 한다.

```bash
rg "192\.168\.67\." .
```

### 6. CI/CD 로그에는 적절한 timeout이 필요하다

네트워크 장애가 발생했을 때 무한히 기다리는 것처럼 보이지 않도록 연결과 전체 요청 시간을 제한해야 한다.

---

## 22. 나중에 스스로 답해볼 질문

아래 질문에 명령어를 보지 않고 답할 수 있으면 이번 작업을 제대로 이해한 것이다.

1. `docker build`와 `docker push`는 각각 어디에 이미지를 만든거나 보내는가?
2. `tag does not exist`는 왜 발생했는가?
3. Docker Hub의 compressed size와 로컬 이미지 크기가 다른 이유는 무엇인가?
4. Public 이미지에 API 키를 넣으면 왜 위험한가?
5. `docker pull`한 이미지는 왜 현재 디렉터리에서 보이지 않는가?
6. `docker image ls`와 `crictl images`의 결과가 다를 수 있는 이유는 무엇인가?
7. master에서 이미지를 pull하면 worker에 자동 복사되는가?
8. Kubernetes는 어느 노드에 이미지를 pull할지 어떻게 결정하는가?
9. `docker` 그룹 추가 후 재로그인이 필요한 이유는 무엇인가?
10. Snap Docker에서 `docker.service`를 찾지 못했던 이유는 무엇인가?
11. GitHub-hosted runner와 self-hosted runner의 역할은 어떻게 다른가?
12. GitHub Actions Secret을 workflow에 어떻게 참조하는가?
13. Prometheus Pod가 정상인데 curl이 실패한 이유는 무엇이었는가?
14. `--connect-timeout`과 `--max-time`은 각각 무엇을 제한하는가?
15. Prometheus를 앱 배포 workflow에서 분리한 이유는 무엇인가?
16. 서버의 `.env.cluster`는 왜 Git에 올리면 안 되는가?

---

## 23. 벨로그에 옮기기 전 확인할 내용

- 실제 명령 출력과 스크린샷에서 토큰 및 API 키 가리기
- Docker Hub 저장소를 Public으로 유지할지 다시 결정
- Prometheus `/-/ready` 실제 응답 캡처
- 성공한 GitHub Actions 화면 캡처
- backend/frontend 최종 화면 캡처
- 이미지 크기 개선 전후를 비교할 경우 같은 명령 기준 사용
- 실제 설치 버전은 게시 직전에 다시 확인

이 글은 완성된 정답이라기보다, KUBEIN을 실제 서버에 올리는 과정에서 발생한 질문과 오류를 다시 이해하기 위한 작업 일지다.
