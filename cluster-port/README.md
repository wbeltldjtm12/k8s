# KubeIn 클러스터 이식 가이드

## 클러스터 구성

| 호스트 | IP | 역할 |
|--------|-----|------|
| master | 192.168.0.12 | Control Plane — **백엔드 여기서 실행** |
| worker1 | 192.168.0.13 | Worker Node |
| worker2 | 192.168.0.14 | Worker Node |

> **아키텍처**: 백엔드(FastAPI)는 master에서 docker-compose로 실행.
> sock-shop은 클러스터 전체(worker1/worker2)에 스케줄링됨.
> Chaos Mesh도 클러스터에 설치됨.

---

## 0. 이 패키지를 master에 복사

**Windows 노트북에서 실행:**
```powershell
# PowerShell에서 현재 저장소 내용을 master의 ~/kubein으로 전송
scp -r "C:\path\to\kubein" user@192.168.0.12:~/
```

> `user` 부분은 master의 실제 리눅스 계정으로 변경 (예: `ubuntu`, `root`, `ch02` 등)

**이후 모든 작업은 master에서 SSH로:**
```bash
ssh user@192.168.0.12
cd ~/kubein
```

---

## 1단계: 런타임 환경 변수 설정

```bash
# 예시를 복사한 뒤 LLM/Prometheus 주소를 환경에 맞게 수정
cp cluster-port/.env.cluster.example cluster-port/.env.cluster
nano cluster-port/.env.cluster
```

> 백엔드는 `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`를 사용하는 OpenAI 호환 클라이언트입니다.
> `KUBEINSIGHT_ENV=prod`가 기본 안전 설정입니다. 인증서 검증을 끄는 개발 클러스터에서만
> 명시적으로 `KUBEINSIGHT_ENV=dev`를 사용하세요.

---

## 2단계: 사전 요구사항 설치

```bash
bash cluster-port/scripts/01-install-prereqs.sh
```

**이 스크립트가 하는 일:**
- Docker / docker-compose 설치 확인 (없으면 자동 설치)
- kubectl → 클러스터 연결 확인 (노드 3개 출력)
- Chaos Mesh 설치 (없으면 helm으로 자동 설치)
- sock-shop 네임스페이스 생성

**예상 출력:**
```
[OK] Docker: Docker version 26.x.x
[OK] 클러스터 연결 정상 (노드 3개)
NAME      STATUS   ROLES           AGE
master    Ready    control-plane   ...
worker1   Ready    <none>          ...
worker2   Ready    <none>          ...
[OK] Chaos Mesh 설치 완료
[OK] sock-shop 네임스페이스 생성
```

> ⚠️ **Chaos Mesh 설치 시 주의**: 클러스터가 `containerd`를 런타임으로 쓰면
> 스크립트가 자동으로 올바른 옵션(`chaosDaemon.runtime=containerd`)을 사용함.
> `docker` 런타임이면 스크립트 안의 helm 명령 수정 필요.

---

## 3단계: sock-shop 배포

```bash
bash cluster-port/scripts/02-deploy-sock-shop.sh
```

**이 스크립트가 하는 일:**
- 공식 microservices-demo의 sock-shop 매니페스트 다운로드
- `kubectl apply`로 클러스터에 배포
- 모든 Pod가 Running 상태가 될 때까지 대기 (최대 5분)

**예상 출력:**
```
[OK] sock-shop 배포 완료
[현재 Pod 상태]
NAME                        READY   STATUS    NODE
carts-xxx                   1/1     Running   worker1
catalogue-xxx               1/1     Running   worker2
front-end-xxx               1/1     Running   worker1
...
[OK] 모든 Pod 정상 Running
```

> ⚠️ **처음 배포 시 이미지 pull 시간**이 걸림 (5~10분).
> "Pending" 상태가 계속 유지되면 `kubectl describe pod <이름> -n sock-shop`으로 원인 확인.

---

## 4단계: 백엔드 실행

```bash
# 저장소 루트에서 백엔드 시작
docker compose --env-file cluster-port/.env.cluster \
  -f cluster-port/docker-compose.cluster.yml up -d

# 로그 확인 (백엔드 정상 기동 확인)
docker compose --env-file cluster-port/.env.cluster \
  -f cluster-port/docker-compose.cluster.yml logs -f
```

**정상 기동 로그:**
```
kubein-backend | [*] kubeconfig 설정 완료 (host network mode)
kubein-backend | [*] KubeInsight 엔진 시작 중...
kubein-backend | INFO:     Uvicorn running on http://0.0.0.0:8000
```

**백엔드 동작 확인:**
```bash
# master에서 직접 확인
curl http://localhost:8000
# → {"status": "ok", "message": "KubeInsight 엔진이 실행 중입니다."}

# K8s 연결 + LLM/ChromaDB 설정 상태 확인
curl http://localhost:8000/api/health | python3 -m json.tool
```

> ⚠️ **kubeconfig 경로 오류 시**: `docker-compose.cluster.yml`의 volume 마운트 확인.
> `${HOME}/.kube/config`가 실제로 존재해야 함.
> ```bash
> ls -la ~/.kube/config   # 이게 있어야 함
> ```

---

## 5단계: 시나리오 파일 경로 맞추기

ablation 스크립트는 `SCENARIO_DIR` 환경변수로 시나리오 위치를 찾음.

> **재현성 주의:** 현재 저장소에는 20개 장애 scenario bundle
> (`inject.sh`, `recover.sh`, `fault.yaml`)이 포함되어 있지 않습니다. 논문 실험에 쓴
> 동일 bundle을 별도로 준비하고, 그 버전/커밋과 archive SHA-256을 실험 기록에 남겨야 합니다.
> bundle의 공식 배포 위치가 정해지기 전에는 이 저장소만으로 end-to-end 재현할 수 없습니다.

필수 구조:
```bash
$SCENARIO_DIR/
├── 01-pod-kill/
│   ├── inject.sh 또는 fault.yaml
│   └── recover.sh                 # 필요한 경우
├── ...
└── 20-multi-fault/
```

절대 경로로 지정:
```bash
export SCENARIO_DIR=/absolute/path/to/sock-shop-scenarios
export GROUND_TRUTH_FILE="$PWD/eval/ground_truth.json"
```

---

## 6단계: 사전 점검

```bash
EXPECTED_NODE_COUNT=3 REQUIRE_CHAOS_MESH=true \
bash cluster-port/scripts/03-pre-check-cluster.sh
```

**모두 통과해야 실험 가능:**
```
[OK]   클러스터 노드 3개 (Ready: 3)
[OK]   sock-shop 안정 상태 (13 pods)
[OK]   Chaos Mesh 전체 Pod 준비 완료 (5 pods)
[OK]   백엔드 API/Kubernetes/ChromaDB 정상 (http://localhost:8000)
[OK]   baseline error_node_count=0 (dfs_only)
[OK]   Ground Truth active 20개 / detectable=10 / 스키마 확인
[OK]   시나리오 디렉터리 20/20
===========================================
 결과: 통과/실패/경고 집계
```

> ⚠️ **`baseline error_node_count` 가 0이 아니면** sock-shop이 아직 불안정한 것.
> `kubectl get pods -n sock-shop`으로 확인 후 모두 Running 될 때까지 대기.

---

## 7단계: 실험 실행

### 단일 사이클 (테스트용)
```bash
bash cluster-port/scripts/04-ablation-eval-cluster.sh
```

### 5사이클 본 실험 (논문용)
```bash
bash cluster-port/scripts/05-ablation-5cycle-cluster.sh
```

주요 실행 profile 변수:

- `EXPECTED_NODE_COUNT` (기본 3), `REQUIRE_CHAOS_MESH` (기본 `true`)
- `SCENARIO_MANIFEST`(기본 `eval/scenarios.txt`), `GROUND_TRUTH_FILE`, `API_BASE`, `EVAL_TIMEOUT_SECONDS`
- `EXCLUDED_SCENARIOS_CSV`: 기본 `02-pod-failure,14-cpu-stress,17-memory-stress`.
  빈 값(`EXCLUDED_SCENARIOS_CSV=`)을 명시하면 기본 제외를 해제합니다.

`detectable=true` 장애를 정상 주입했지만 끝까지 찾지 못한 경우는 실험 오류가 아니라
`status=valid`, `detected=false` false negative로 기록되어 다음 cycle을 계속합니다.

**실험 진행 중 출력 예시:**
```
==========================================
 [1/20] — 01-pod-kill
==========================================
  [대기] deployment / pod 안정화 확인
  [OK] 클러스터 안정
  [OK] baseline=0
[주입] 01-pod-kill
  [스킵] Ground Truth detectable=false
  [동일 snapshot 3모드 평가]
    [hybrid]   total_chains=2
    [dfs_only] total_chains=2
    [llm_only] ai_analysis 길이=847
[복구] 01-pod-kill
  [OK] 클러스터 안정
  [OK] baseline=0

==========================================
 [2/20] — 02-pod-failure
...
```

**결과 파일 위치:**
```
$SCENARIO_DIR/results/
└── ablation_20260522_150000_a1B2c3/  ← 단일 cycle 고유 결과
    ├── 01-pod-kill/
    │   ├── hybrid.json
    │   ├── dfs_only.json
    │   ├── llm_only.json
    │   ├── inject.log
    │   └── status.txt
    ├── ...
    └── _summary.tsv              ← 20개 시나리오 요약

ablation_5cycle_merged_20260522_XXXXXX/
├── _merged_summary.tsv           ← status/detectable/detected 통합 요약
└── _cycle_dirs.txt               ← 정확히 사용한 cycle 경로 manifest
```

---

## 실험 후: 결과 분석

```bash
cd ~/kubein

# 05가 출력한 merge 디렉터리 사용
MERGE_DIR="$SCENARIO_DIR/results/ablation_5cycle_merged_..."
mapfile -t CYCLE_DIRS < "$MERGE_DIR/_cycle_dirs.txt"
python3 eval/compare-5cycle.py "${CYCLE_DIRS[@]}"

# Blast Radius 평가: 단일 cycle 결과 디렉터리
python3 eval/eval-blast-radius.py "${CYCLE_DIRS[0]}"
```

---

## 자주 발생하는 문제

### ❌ `kubectl` 권한 없음 (docker 컨테이너 내부에서)
```
Error: forbidden: User cannot list resource
```
→ master의 `~/.kube/config`가 admin 권한인지 확인:
```bash
kubectl auth can-i list pods --all-namespaces
# → yes 여야 함
```

### ❌ Chaos Mesh fault.yaml apply 실패
```
error: no kind "PodChaos" is registered
```
→ Chaos Mesh CRD가 설치 안 된 것. `01-install-prereqs.sh` 재실행.

### ❌ baseline이 계속 0이 안 됨
```
[FAIL] baseline error_node_count=3
```
→ sock-shop에 문제 있는 Pod 확인:
```bash
kubectl get pods -n sock-shop | grep -v Running
kubectl describe pod <문제있는-pod> -n sock-shop | tail -20
```

### ❌ 백엔드 API 응답 없음
```
[FAIL] 백엔드 API 응답 실패
```
→ 컨테이너 상태 확인:
```bash
docker ps | grep kubein
docker logs kubein-backend --tail 30
```
