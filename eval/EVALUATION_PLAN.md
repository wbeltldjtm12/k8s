# KubeIn 논문/연구 평가(Evaluation) 파이프라인 계획서

본 문서는 KubeIn 시스템의 Root Cause Analysis 정확도, 처리 속도, 그리고 장애 파급 효과 추적 능력을 다각도로 검증하기 위한 5단계 평가 마스터 플랜입니다.

---

## 1. 단일 사이클 테스트 (`cluster-port/scripts/04-ablation-eval-cluster.sh`)

- **목표**: KubeIn의 Error Graph 메커니즘이 20가지 장애 상황을 재현/포착하고, 3가지 모드가 정상적으로 동일 환경(Snapshot)을 평가하는지 확인
- **시나리오 원본 목록**: `eval/scenarios.txt`를 pre-check, 단일 cycle, 5-cycle 검증이 공동 사용
- **규모**: 제외가 없으면 20 × 3 = 60회 mode 평가. 현재 기본 제외 3건 적용 시 17 × 3 = 51회
- **파이프라인 흐름**:
  1. 모든 Deployment와 Pod가 Ready가 될 때까지 대기 (각 `kubectl wait` 최대 180초)
  2. 잔여 ReplicaSet 삭제 등으로 환경 초기화
  3. `inject.sh` 실행하여 Chaos Mesh 및 자원 파괴 에러 주입
  4. 5초 대기 후 에러 전파 폴링 (15회, 각 API timeout과 재시도 간격 포함)
  5. 식별된 장애 상태에서 평가 snapshot을 1회 생성하여 불변 `ResourceCache`와 `DependencyGraph`를 발급
  6. 동일한 `snapshot_id`로 Hybrid, DFS-Only, LLM-Only 모드를 순차 호출하고 응답 ID 일치 검증 후 결과 JSON 저장
  7. `recover.sh` 실행하여 장애 복구
  8. Deployment/Pod 준비 상태와 `baseline error_node_count=0` 복귀 확인
- **산출물**: `results/ablation_YYYYMMDD_HHMMSS_XXXXXX/` 고유 디렉터리와 검증된 결과 JSON/summary
- **trial 판정**:
  - 주입, API, snapshot, 산출물 저장, 복구 실패는 `invalid`이며 반복 실행을 중단
  - 정상 실행에서 장애를 못 찾은 경우는 `valid`, `detected=false`인 false negative로 보존
  - `EXCLUDED_SCENARIOS_CSV`의 항목은 `excluded`이며 정량 분모에서 제외하되 coverage에 반영

> 여기서 동일 snapshot은 Kubernetes 전체의 원자적 트랜잭션이 아니라, 한 번 수집해 고정한 동일 관측본을 세 모드가 공유한다는 뜻입니다.
> Snapshot 저장소는 현재 단일 Uvicorn 프로세스 메모리에 있습니다. worker/replica를 늘리려면
> 공유 저장소 또는 sticky routing을 먼저 구현해야 합니다.

---

## 2. 5-Cycle 본 실험 (`cluster-port/scripts/05-ablation-5cycle-cluster.sh`)

- **목표**: 논문 작성 시 필수적인 **통계적 유의성 및 실험 신뢰성** 확보
- **규모**: 단일 테스트 5회 반복. 제외가 없으면 100개 trial/300회 mode 평가이며,
  현재 기본 제외 정책에서는 85개 평가 trial/255회 mode 평가
- **파이프라인 흐름**: 
  - 이전 단계 결과의 노이즈(예: Kubernetes 복구 지연)를 차단하기 위해 사이클 종료 시마다 **60초 클러스터 휴지기** 강제
  - 무인 자동화 진행 (진행 경과 로그 기록 및 최종 생성 디렉터리 5개 반환)

---

## 3. 정량적 정확도 분석 (`compare-5cycle.py`)

- **목표**: 탐지 가능성(Hit@1)과 소요 시간 관점에서의 모델 별 정량지표 도출
- **기준 지표**: active 20개 시나리오 중 `ground_truth.json`의 `detectable=true` 10건이 명목 대상. 비교기는 `status.txt`를 읽어 `valid` trial만 정확도 분모에 넣고, excluded/invalid를 별도 coverage로 보고
- **기본 제외 정책**: 현재 runner 기본값은 `02-pod-failure`, `14-cpu-stress`, `17-memory-stress`. 이 중 `02-pod-failure`는 Ground Truth상 detectable이므로 기본 실행의 정량 coverage는 최대 9/10입니다. 해당 scenario bundle이 실제 주입을 지원하면 `EXCLUDED_SCENARIOS_CSV=`로 기본 제외를 해제해야 합니다.
- **LLM-Only 해석**: 비어 있지 않은 응답 생성률은 가용성 지표일 뿐 정확도가 아님. 내용 정확도는 `judge/`의 다중 LLM 판정 결과로 별도 보고
- **산출 통계**:
  - 각 모드(Hybrid / DFS / LLM)별 평균 Hit 정확도(%) ± 5사이클 표준편차
  - 각 분석에 소요된 평균 시간(Sec) ± 표준편차
- **예상 출력**: 논문 Table 용도의 직관적 포맷 (예: `Hybrid: 85.7% ± 3.2%`, `DFS-Only: 78.6% ± 4.1%` 등)

---

## 4. LLM-as-a-Judge 정성 평가 (`judge.py`)

- **목표**: 기계적 문자열 매칭(Hit@1)에서 놓칠 수 있는 **분석 결과의 설명력과 질적 수준 판별**
- **평가자**: Gemini, GPT, Claude의 다중 판정 후 evaluator별 점수와 ensemble 평균 보고
- **채점 구조 (블라인드 테스트)**:
  - 3모드의 결과를 A, B, C 레이블에 무작위 셔플링하여 LLM의 Label Bias 방지
  - 시스템에는 실제 장애 발생 내용(Ground Truth Description)만 공급
  - 각 모드 결과에 대해 절대평가 (0점 ~ 10점):
    - `10점`: 실제 장애 메커니즘 완벽하게 추론 및 근본 원인 정확히 식별
    - `7~9점`: 정답의 기저 원인에 매우 근접함
    - `4~6점`: 표면적인 에러 증상만 캐치하고 전파 경로는 놓침
    - `0~3점`: 완전한 분석 실패 및 엉뚱한 결론

실행 시 cycle 디렉터리를 명시적으로 전달합니다. 판정 대상은 로컬 Ground Truth의
`detectable=true`와 각 trial의 `status=valid`에서 동적으로 정해집니다.

```bash
python3 judge/judge.py "${CYCLE_DIRS[@]}"
```

기존 `judge_results_5cycle.json`은 보존 결과이며, 새 실행은 기본적으로 timestamp가 붙은
별도 파일에 저장됩니다(`JUDGE_OUTPUT`으로 경로 지정 가능).

---

## 5. [신규] Blast Radius (영향 범위) 정밀 평가

- **배경**:
  - 기존 K8s 장애 진단 논문(예: MicroRCA, RunD 등)은 보통 출발점(Root Cause)을 찾았느냐(Hit@1 / Hit@5)에만 집중합니다.
  - 하지만 KubeIn의 가장 큰 강점은 **장애가 어디로 파급되는지(Blast Radius) 추적하는 역방향 전파 탐색(역 BFS)** 엔진에 있습니다.
- **실행 계획 (To-Do)**:
  1. `ground_truth.json` 내 각 시나리오별로 장애의 영향을 받는 대상들(`expected_affected`) 수동 정의.
  2. KubeIn 결과의 `affected_node_ids` 배열과 교차 비교 스크립트 작성 (`eval/eval-blast-radius.py`).
  3. **Precision (정밀도)**: 시스템이 "이들이 피해를 입었다"라고 주장한 리소스 중 진짜 영향을 받은 노드의 비율 산출.
  4. **Recall (재현율)**: 실제로 피해를 입은 K8s 리소스 중 KubeIn이 전파 그래프로 찾아낸 리소스 비율 산출.
  5. 이를 통해 '원인뿐만 아니라 후폭풍까지 선제 계산하는 최초의 AIOps 시스템'임을 논리적으로 증명.
