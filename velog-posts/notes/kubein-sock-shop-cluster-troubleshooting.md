# KUBEIN 실험 환경 구축 기록: Docker, Namespace, 디스크 부족, Sock Shop

> 작성일: 2026-07-24  
> 목적: KUBEIN 연구용 Kubernetes 실험 환경을 구성하며 겪은 문제와 해결 과정을 다시 이해하기 위한 기록  
> 주의: API 키, 토큰, 비밀번호, kubeconfig 내용은 기록하지 않는다.

## 1. 어디까지 진행했는가

이번 작업에서는 다음 환경을 구성했다.

```text
Proxmox
└─ Kubernetes 클러스터
   ├─ k8s-master  : 192.168.0.12
   ├─ k8s-worker1 : 192.168.0.13
   └─ k8s-worker2 : 192.168.0.14
```

클러스터에는 다음 시스템이 실행 중이다.

```text
kube-system
└─ Kubernetes Control Plane, CoreDNS, kube-proxy

calico-system / tigera-operator
└─ Calico 네트워크

monitoring
└─ Prometheus

sock-shop
└─ KUBEIN 장애 실험 대상 애플리케이션
```

KUBEIN 자체는 아직 Kubernetes Pod로 배포하지 않았다.
GitHub Actions가 master 서버의 Docker Compose를 실행하는 구조다.

```text
k8s-master
└─ Docker Engine
   ├─ kubein-backend
   └─ kubein-frontend
```

따라서 다음 명령에는 KUBEIN이 나오지 않는다.

```bash
kubectl get pods -A
```

KUBEIN 컨테이너는 다음 명령으로 확인한다.

```bash
docker ps --filter name=kubein
```

---

## 2. CI/CD가 성공하면 엔진 검증도 끝난 것인가?

GitHub Actions를 통해 다음 과정은 성공했다.

```text
GitHub push
→ backend/frontend 이미지 빌드
→ Docker Hub push
→ self-hosted runner가 master에서 이미지 pull
→ Docker Compose 재배포
→ HTTP health check
```

하지만 CI/CD 성공과 RCA 엔진의 정답성은 다른 문제다.

CI/CD가 검증하는 것:

- 코드가 이미지로 빌드되는가?
- Docker Hub에 push되는가?
- 서버에서 컨테이너가 실행되는가?
- HTTP 서버가 응답하는가?

논문 실험에서 검증해야 하는 것:

- Kubernetes 장애를 탐지하는가?
- 올바른 근본 원인을 찾는가?
- Ground Truth와 얼마나 일치하는가?
- 같은 입력에서 비교 모드가 공정하게 실행되는가?
- 결과가 반복 실험에서도 재현되는가?

그래서 연구 실험의 중심을 CI/CD가 아니라 Python 엔진 직접 실행으로 다시 정했다.

```text
장애 주입
→ Python 엔진 1회 실행
→ JSON 결과 저장
→ Ground Truth 비교
→ 장애 복구
```

CI/CD는 삭제하지 않고 웹 데모와 배포 편의를 위한 보조 수단으로 남긴다.

---

## 3. `python main.py`와 FastAPI 실행의 차이

기존에는 다음과 같이 실행했다.

```bash
cd backend
python main.py
```

`main.py`의 마지막에는 다음 구조가 있다.

```python
if __name__ == "__main__":
    run_analysis()
```

따라서 `python main.py`는 다음 전체 파이프라인을 한 번 실행하고 종료한다.

```text
Kubernetes 리소스 수집
→ 개별 장애 분석
→ 의존성 그래프 구성
→ RCA 실행
→ Kubernetes Events 수집
→ Prometheus 조회
→ RAG 검색
→ LLM 설명 생성
→ 결과 출력
```

반면 Docker 컨테이너에서는 다음 프로세스가 실행된다.

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

이 경우 엔진이 계속 분석하는 것이 아니다.
FastAPI 서버가 요청을 기다리다가 특정 API가 호출될 때 분석을 실행한다.

```text
/api/causality_fast
→ LLM 없이 그래프 RCA

/api/analyze
→ RCA + Prometheus + RAG + LLM

/api/evaluate
→ 논문 평가용 비교 모드
```

Pod나 컨테이너에 올렸다는 사실만으로 엔진의 정확도가 검증되지는 않는다.

---

## 4. 직접 실행용 Python CLI 추가

FastAPI와 HTTP를 거치지 않고 엔진을 직접 실행하고 JSON 결과를 저장하도록
다음 파일을 추가했다.

```text
backend/engine_cli.py
```

지원 모드:

| 모드 | 설명 |
| --- | --- |
| `full` | 기존 `python main.py` 전체 파이프라인 |
| `hybrid` | 그래프 RCA와 LLM 설명 |
| `dfs_only` | LLM 없이 DFS 기준 RCA |
| `llm_only` | 그래프 RCA 없이 LLM 기준 결과 |
| `all` | 동일 snapshot으로 세 비교 모드 실행 |

실행 예시:

```bash
cd ~/kubein/backend

python engine_cli.py \
  --mode dfs_only \
  --env-file /home/master/kubein-config/.env.cluster \
  --kubeconfig /home/master/.kube/config
```

다음 정보가 JSON으로 저장된다.

- 실행 ID
- 시작 및 종료 시각
- 실행 시간
- Git commit SHA
- 실행 모드
- LLM 모델 이름
- Prometheus 주소
- RCA 결과
- 성공 또는 실패 상태

API 키와 토큰 값은 저장하지 않는다.

현재 직접 실행기 커밋은 로컬에만 있으며 아직 원격 저장소에는 push하지 않았다.

```text
af08ca4 Add direct Python engine validation CLI
```

---

## 5. Kubernetes Namespace가 필요한 이유

`kubectl get pods -A`를 실행하면 모든 namespace의 Pod가 섞여서 출력된다.

```text
calico-system
kube-system
monitoring
sock-shop
tigera-operator
```

각 namespace의 용도는 다음과 같다.

| Namespace | 역할 |
| --- | --- |
| `kube-system` | Kubernetes 핵심 구성요소 |
| `calico-system` | Calico 네트워크 구성요소 |
| `tigera-operator` | Calico 설치 및 상태 관리 |
| `monitoring` | Prometheus stack |
| `sock-shop` | KUBEIN 장애 실험 대상 |

특정 namespace만 조회하려면:

```bash
kubectl get pods -n sock-shop
```

Pod를 상세 조회할 때도 namespace를 지정해야 한다.

```bash
kubectl describe pod <POD_NAME> -n sock-shop
```

현재 디렉터리 이름이 `sock-shop`이라고 해서 kubectl의 namespace가 자동으로
바뀌는 것은 아니다.

반복 입력이 불편하면 현재 context의 기본 namespace를 설정할 수 있다.

```bash
kubectl config set-context --current --namespace=sock-shop
```

확인:

```bash
kubectl config view --minify | grep namespace
```

이후에는 다음처럼 사용할 수 있다.

```bash
kubectl get pods
kubectl get services
kubectl describe pod <POD_NAME>
```

다른 namespace를 잠깐 확인할 때만 `-n`을 붙인다.

```bash
kubectl get pods -n monitoring
kubectl get pods -n calico-system
```

---

## 6. Tigera와 Calico는 무엇인가?

Tigera Operator는 Calico 네트워크 구성요소를 설치하고 원하는 상태로 유지하는
Kubernetes Operator다.

```text
Tigera Operator
└─ Calico 관리
   ├─ calico-node
   ├─ calico-kube-controllers
   ├─ calico-apiserver
   ├─ calico-typha
   ├─ Whisker
   └─ Goldmane
```

Calico는 Pod 네트워크와 네트워크 정책을 담당한다.
따라서 `calico-system`과 `tigera-operator`의 정상 실행 중인 Pod를 임의로
삭제하면 안 된다.

Deployment가 새 Pod로 교체된 뒤 남은 `Completed`, `Evicted`,
`ContainerStatusUnknown` Pod는 현재 복제본이 정상인지 확인한 후 정리할 수 있다.

예를 들어 Calico API server의 정상 복제본을 먼저 확인한다.

```bash
kubectl get deployment calico-apiserver -n calico-system
```

`READY 2/2`이고 새로운 두 Pod가 `Running`일 때만 오래된 실패 Pod를 삭제한다.

```bash
kubectl delete pod \
  <OLD_FAILED_POD_1> \
  <OLD_FAILED_POD_2> \
  -n calico-system
```

---

## 7. Sock Shop 공식 매니페스트 다운로드

Sock Shop은 여러 마이크로서비스로 구성된 예제 애플리케이션이다.
KUBEIN의 장애 주입 및 RCA 평가 대상으로 사용한다.

이번에는 실수로 바로 배포하지 않도록 매니페스트 다운로드와 실제 적용을
분리했다.

다운로드:

```bash
mkdir -p ~/kubein-lab/manifests/sock-shop

curl -fL \
  https://raw.githubusercontent.com/microservices-demo/microservices-demo/master/deploy/kubernetes/complete-demo.yaml \
  -o ~/kubein-lab/manifests/sock-shop/complete-demo.yaml
```

실제 변경 없이 문법과 리소스 생성 가능 여부 확인:

```bash
kubectl apply --dry-run=client \
  -f ~/kubein-lab/manifests/sock-shop/complete-demo.yaml
```

실제 배포:

```bash
kubectl apply \
  -f ~/kubein-lab/manifests/sock-shop/complete-demo.yaml
```

상태 확인:

```bash
kubectl get pods -n sock-shop -w
```

---

## 8. YAML 파일 하나로 여러 리소스가 생성되는 이유

`kubectl apply` 명령에 Deployment나 Service를 직접 적지 않아도 YAML 내부에
리소스 종류가 들어 있다.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: carts
  namespace: sock-shop
spec:
  replicas: 1
```

각 필드의 의미:

```text
apiVersion → 사용할 Kubernetes API 버전
kind       → Namespace, Deployment, Service 등의 리소스 종류
metadata   → 이름과 namespace
spec       → 원하는 리소스 상태
```

한 파일에 여러 리소스가 있을 때는 `---`로 구분한다.

```text
Namespace
---
Deployment
---
Service
---
다음 Deployment
```

Deployment를 생성하면 최종적으로 다음 순서로 Pod가 생성된다.

```text
Deployment
→ ReplicaSet
→ Pod
```

YAML의 `metadata.namespace`와 명령의 `-n`이 모두 없다면 일반적인 namespaced
리소스는 현재 context의 기본 namespace에 생성된다.

---

## 9. Pod가 Evicted된 원인 찾기

Calico Whisker Pod를 상세 조회했을 때 다음 내용이 확인됐다.

```text
Status:  Failed
Reason:  Evicted
Message: The node was low on resource: ephemeral-storage
Node:    k8s-master
```

중요한 점은 Whisker 애플리케이션 자체의 오류가 아니라 master 노드의 디스크
공간 부족이 원인이었다는 것이다.

`ephemeral-storage`에는 주로 다음 항목이 포함된다.

- containerd 이미지와 컨테이너 레이어
- 컨테이너 로그
- 컨테이너 writable layer
- `emptyDir`
- 각종 임시 파일

메모리 부족과는 다르므로 `free -h`가 아니라 `df -h`를 먼저 확인해야 한다.

```bash
df -h /
```

처음 확인한 상태:

```text
Filesystem                         Size  Used  Avail  Use%
/dev/mapper/ubuntu--vg-ubuntu--lv   15G   13G   1.3G   91%
```

루트 파일시스템에 1.3GB만 남아 Kubernetes의 eviction threshold 아래로
내려간 상태였다.

이 때문에 다음과 같은 Pod가 퇴거됐다.

- Calico Whisker
- Prometheus node-exporter
- 기타 BestEffort Pod

`QoS Class: BestEffort`이고 ephemeral-storage request가 없는 Pod가 우선적인
퇴거 대상이 될 수 있다.

---

## 10. VM에는 60GB를 줬는데 Ubuntu에는 왜 15GB만 보였는가?

`lsblk`, `vgs`, `lvs` 결과는 다음과 같았다.

```text
sda                         32G
└─ sda3                     30G
   └─ ubuntu-vg
      ├─ ubuntu-lv          15G
      └─ VFree              15G
```

여기서 두 가지를 확인했다.

1. Ubuntu VM이 실제로 인식한 가상 디스크는 32GB였다.
2. 30GB LVM Volume Group 중 루트 Logical Volume에는 15GB만 할당돼 있었다.

`df -h`는 가상 디스크 전체가 아니라 현재 파일시스템에 할당된 크기를 표시한다.

확인 명령:

```bash
lsblk
sudo vgs
sudo lvs
```

LVM의 남은 15GB를 루트에 온라인 확장했다.

```bash
sudo lvextend \
  -l +100%FREE \
  -r \
  /dev/ubuntu-vg/ubuntu-lv
```

옵션:

```text
-l +100%FREE → Volume Group의 남은 공간 전부 사용
-r           → Logical Volume과 파일시스템을 함께 확장
```

확장 후:

```text
Size:  30GB
Used:  13GB
Avail: 16GB
Use%:  45%
```

```bash
df -h /
```

서버 재부팅이나 Kubernetes 중지는 필요하지 않았다.

다만 Proxmox에서 60GB를 설정했다고 생각했는데 guest의 `sda`가 32GB로
보이는 문제는 아직 확인이 필요하다. Proxmox의 실제 VM 디스크 설정 또는
디스크 확장 적용 여부를 나중에 점검해야 한다.

---

## 11. `kubectl logs --previous`가 실패한 이유

CrashLoopBackOff Pod의 이전 컨테이너 로그를 확인하기 위해 다음 명령을 사용했다.

```bash
kubectl logs -n sock-shop deployment/carts-db --previous
```

그러나 containerd에서 이전 컨테이너 로그를 가져오지 못했다.

```text
unable to retrieve container logs
```

디스크 부족 상황에서 과거 컨테이너 또는 로그가 이미 정리됐을 가능성이 있다.
디스크를 확장한 뒤 현재 컨테이너 로그를 다시 조회했다.

```bash
kubectl logs -n sock-shop deployment/orders-db --tail=100
```

Pod Events와 컨테이너 로그의 역할도 다르다.

```text
kubectl describe pod
→ 스케줄링, 이미지 pull, 컨테이너 생성, 재시작 등 Kubernetes Events

kubectl logs
→ 애플리케이션 프로세스가 출력한 실제 오류
```

`BackOff restarting failed container`는 현상이고, 실제 종료 원인은 컨테이너
로그에서 확인해야 한다.

---

## 12. MongoDB가 CrashLoopBackOff된 원인

Sock Shop의 다음 두 Pod가 반복적으로 종료됐다.

```text
carts-db
orders-db
```

로그:

```text
MongoDB 5.0+ requires a CPU with AVX support
current system does not appear to have AVX support
```

공식 Sock Shop 매니페스트에는 MongoDB 이미지 버전이 고정돼 있지 않았다.

```yaml
image: mongo
```

이 설정은 배포 시점의 최신 MongoDB 이미지를 받는다.
최신 이미지가 MongoDB 5.0 이상으로 변경되면서 AVX가 노출되지 않은 VM에서
실행할 수 없게 됐다.

이번 환경의 물리 CPU가 AVX를 지원하더라도 Proxmox VM CPU 유형에 따라
guest에 AVX 플래그가 전달되지 않을 수 있다.

가장 단순하고 재현 가능한 해결책으로 MongoDB 4.4를 사용했다.

```bash
kubectl set image deployment/carts-db \
  carts-db=mongo:4.4 \
  -n sock-shop

kubectl set image deployment/orders-db \
  orders-db=mongo:4.4 \
  -n sock-shop
```

변경 상태 확인:

```bash
kubectl rollout status deployment/carts-db \
  -n sock-shop \
  --timeout=180s

kubectl rollout status deployment/orders-db \
  -n sock-shop \
  --timeout=180s
```

`kubectl set image`만 사용하면 현재 Deployment만 변경된다.
나중에 원본 YAML을 다시 적용하면 `image: mongo`로 돌아갈 수 있으므로
다운로드한 매니페스트도 수정해야 한다.

```bash
sed -i 's/image: mongo$/image: mongo:4.4/' \
  ~/kubein-lab/manifests/sock-shop/complete-demo.yaml

grep 'image: mongo' \
  ~/kubein-lab/manifests/sock-shop/complete-demo.yaml
```

장기적으로는 `mongo:4.4`보다 정확한 patch 버전이나 image digest까지 고정하는
것이 재현성에 더 유리하다.

현재 `carts-db`와 `orders-db`가 최종적으로 `Running`이 됐는지는 다음 작업에서
확인해야 한다.

---

## 13. `sudo kubectl`이 localhost:8080에 접속한 이유

일반 사용자로 실행한 명령은 성공했다.

```bash
kubectl set image ...
```

같은 명령에 `sudo`를 붙이자 다음 오류가 발생했다.

```text
The connection to the server localhost:8080 was refused
```

원인은 사용자별 kubeconfig 경로다.

```text
master 사용자
└─ /home/master/.kube/config 존재

sudo 실행
└─ root 사용자로 전환
   └─ /root/.kube/config 없음
      └─ 기본 localhost:8080 접속 시도
```

현재 `master` 사용자는 이미 Kubernetes 접근 권한이 있으므로 `kubectl`에는
`sudo`를 붙이지 않는다.

```bash
kubectl get nodes
kubectl get pods -n sock-shop
```

`sudo`가 필요한 Linux 파일 작업과 Kubernetes API 명령을 구분해야 한다.

---

## 14. 오래된 nodeSelector 경고

이미지를 변경할 때 다음 경고가 나타났다.

```text
spec.template.spec.nodeSelector[beta.kubernetes.io/os] is deprecated
use kubernetes.io/os instead
```

기존 Sock Shop 매니페스트가 다음 오래된 label을 사용하기 때문이다.

```yaml
nodeSelector:
  beta.kubernetes.io/os: linux
```

현재 권장 형식:

```yaml
nodeSelector:
  kubernetes.io/os: linux
```

이 메시지는 경고이며 이번 이미지 변경이 실패했다는 뜻은 아니다.
나중에 로컬 매니페스트를 정리할 때 전체 항목을 현재 label로 교체할 예정이다.

---

## 15. 현재 확인해야 할 상태

작업을 재개할 때 다음 순서로 확인한다.

### 1. 디스크

```bash
df -h /
```

기대 상태:

```text
Size 약 30GB
Avail 약 16GB
Use% 약 45%
```

### 2. Sock Shop

```bash
kubectl get pods -n sock-shop
```

특히 확인:

```text
carts-db   1/1 Running
orders-db  1/1 Running
```

### 3. Prometheus node-exporter

```bash
kubectl get daemonset,pods -n monitoring \
  -l app.kubernetes.io/name=prometheus-node-exporter \
  -o wide
```

디스크 확장 후 세 노드에 node-exporter가 정상 실행되는지 확인한다.

### 4. Calico API server

```bash
kubectl get deployment,pods \
  -n calico-system \
  -l k8s-app=calico-apiserver
```

정상 복제본이 `2/2`인지 확인한 뒤 오래된 `ContainerStatusUnknown` Pod만
정리한다.

---

## 16. 아직 남은 연구 환경 작업

- `carts-db`, `orders-db`가 MongoDB 4.4로 정상 실행되는지 확인
- 원본 Sock Shop YAML에도 MongoDB 버전 고정
- deprecated `beta.kubernetes.io/os` label 변경
- 디스크 부족으로 남은 Evicted 및 Unknown Pod 정리
- Proxmox 디스크가 60GB가 아니라 32GB로 보이는 원인 확인
- 직접 Python 엔진을 원격 저장소와 서버에 반영
- 엔진에 `--namespace sock-shop` 범위 옵션 추가
- 정상 상태 baseline 저장
- 단일 장애 시나리오 주입 및 RCA 결과 확인
- 20개 장애 시나리오 위치 확인 또는 복원
- Ground Truth 기반 정확도 평가

특히 현재 KUBEIN의 `ResourceCache`는 다음 API를 사용해 전체 namespace를
수집한다.

```python
list_pod_for_all_namespaces()
```

시스템 namespace 제외 목록에도 `calico-system`, `tigera-operator`,
`monitoring`이 포함돼 있지 않아 시스템 장애가 실험 결과에 섞일 가능성이 있다.

따라서 정확도 실험 전에 다음과 같은 명시적인 분석 범위를 추가해야 한다.

```bash
python engine_cli.py \
  --namespace sock-shop \
  --mode dfs_only
```

이 기능은 아직 구현되지 않았다.

---

## 17. 자주 사용할 명령어

### 전체 namespace 확인

```bash
kubectl get namespaces
```

### Sock Shop만 확인

```bash
kubectl get pods -n sock-shop
```

### 모든 비정상 Pod 확인

```bash
kubectl get pods -A \
  --field-selector=status.phase!=Running,status.phase!=Succeeded \
  -o wide
```

### Pod 이벤트 확인

```bash
kubectl describe pod <POD_NAME> -n <NAMESPACE>
```

### 현재 컨테이너 로그

```bash
kubectl logs <POD_NAME> -n <NAMESPACE> --tail=100
```

### 이전에 종료된 컨테이너 로그

```bash
kubectl logs <POD_NAME> -n <NAMESPACE> --previous --tail=100
```

### 배포 이미지 확인

```bash
kubectl get deployment <DEPLOYMENT> \
  -n <NAMESPACE> \
  -o jsonpath='{.spec.template.spec.containers[*].image}'
```

### master의 KUBEIN Docker 컨테이너 확인

```bash
docker ps --filter name=kubein
```

### 디스크 확인

```bash
df -h /
lsblk
sudo vgs
sudo lvs
```

---

## 18. 이번 작업에서 배운 점

### 1. 배포 성공과 알고리즘 검증은 다르다

컨테이너가 실행되고 HTTP 200을 반환해도 RCA 정답률은 검증되지 않는다.
연구에서는 장애 입력, 엔진 출력, Ground Truth 비교가 핵심이다.

### 2. Kubernetes Pod와 Docker 컨테이너를 구분해야 한다

Docker Compose로 실행한 KUBEIN은 `kubectl get pods`에 나오지 않는다.
같은 master에서 실행되더라도 관리 주체가 다르다.

### 3. namespace는 디렉터리가 아니다

현재 Linux 작업 디렉터리가 `sock-shop`이어도 kubectl namespace는 바뀌지 않는다.
`-n`, 현재 context 또는 YAML의 `metadata.namespace`가 namespace를 결정한다.

### 4. Events는 현상, 로그는 애플리케이션 원인을 보여준다

`CrashLoopBackOff`와 `BackOff`만으로는 프로세스가 왜 종료됐는지 알 수 없다.
실제 원인은 `kubectl logs`로 확인해야 한다.

### 5. `latest` 또는 버전 없는 이미지는 재현성을 깨뜨린다

오래된 Sock Shop YAML의 `image: mongo`가 최신 MongoDB를 받아 AVX 오류를
발생시켰다. 실험 환경의 모든 이미지는 버전 또는 digest를 고정해야 한다.

### 6. VM 디스크 크기와 파일시스템 크기는 다를 수 있다

가상 디스크, 파티션, LVM Volume Group, Logical Volume, 파일시스템은 각각
별도 계층이다. `df`, `lsblk`, `vgs`, `lvs`를 함께 확인해야 한다.

### 7. Pod 장애의 진짜 원인이 애플리케이션이 아닐 수 있다

Whisker는 수백 KiB만 사용했지만 노드 전체의 ephemeral-storage 부족 때문에
Evicted됐다. Pod 상태만 보지 말고 Node와 인프라 상태도 확인해야 한다.

### 8. `sudo`는 모든 권한 문제의 해결책이 아니다

`sudo kubectl`은 root 사용자의 kubeconfig를 찾기 때문에 오히려 클러스터 연결이
실패했다. 명령이 어떤 사용자 환경과 설정 파일을 사용하는지 이해해야 한다.

---

## 19. 다음 작업을 시작할 때

다음 세 명령부터 실행한다.

```bash
df -h /
kubectl get pods -n sock-shop
kubectl get pods -n monitoring
```

그 후 다음 목표 하나에만 집중한다.

```text
carts-db와 orders-db를 포함한 모든 Sock Shop Pod를 Running 상태로 만들기
```

클러스터가 정상 baseline에 도달하기 전에는 장애 주입이나 RCA 정확도 평가를
시작하지 않는다.
