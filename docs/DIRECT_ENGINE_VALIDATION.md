# KUBEIN 직접 Python 엔진 검증

KUBEIN 연구 실험의 기준 실행 경로는 웹 배포가 아니라 다음 순서입니다.

```text
장애 주입
  → Python 엔진 직접 실행
  → JSON 결과 저장
  → Ground Truth 비교
  → 장애 복구
```

GitHub Actions와 Docker Compose 배포는 데모 및 원격 실행 환경을 유지하는
보조 수단입니다. 엔진의 탐지율과 RCA 정답률은 직접 실행 결과로 평가합니다.

## 기존 `main.py` 방식

기존 전체 파이프라인을 한 번 실행하려면 backend 디렉터리에서 다음 명령을
사용합니다.

```bash
python main.py
```

이 명령은 Kubernetes 리소스 수집, 개별 장애 탐지, 의존성 그래프 RCA,
Events, Prometheus, RAG 및 LLM 분석을 순서대로 실행하고 종료합니다.

## 결과를 JSON으로 저장하는 방식

`engine_cli.py`는 FastAPI나 Uvicorn을 거치지 않고 엔진 함수를 직접 호출합니다.

서버의 소스 체크아웃에서 실행:

```bash
cd ~/kubein/backend

python engine_cli.py \
  --mode full \
  --env-file /home/master/kubein-config/.env.cluster \
  --kubeconfig /home/master/.kube/config
```

배포된 backend 컨테이너 안에서 실행:

```bash
docker exec kubein-backend \
  python /app/engine_cli.py --mode full
```

결과는 기본적으로 저장소의 `engine-results/` 아래에 JSON으로 저장됩니다.
저장 위치를 지정하려면:

```bash
python engine_cli.py \
  --mode dfs_only \
  --output /home/master/kubein-results/smoke-dfs.json
```

## 실행 모드

| 모드 | 용도 |
| --- | --- |
| `full` | 기존 `python main.py` 전체 파이프라인 |
| `hybrid` | DFS+BFS RCA와 LLM 설명 |
| `dfs_only` | LLM 없이 DFS 기준 RCA 검증 |
| `llm_only` | 그래프 RCA 없이 LLM 기준 결과 |
| `all` | 한 번 수집한 동일 snapshot으로 세 비교 모드 실행 |

정답률을 먼저 확인할 때는 외부 LLM 변동이 없는 `dfs_only`부터 사용합니다.
세 모드의 공정한 비교에는 `all`을 사용합니다.

```bash
python engine_cli.py \
  --mode all \
  --env-file /home/master/kubein-config/.env.cluster \
  --kubeconfig /home/master/.kube/config \
  --output /home/master/kubein-results/scenario-04.json
```

## JSON에 기록되는 정보

- 실행 고유 ID
- 시작 및 종료 시각
- 실행 시간
- Git commit SHA
- 선택한 모드
- kubeconfig 경로
- LLM 모델 이름
- Prometheus 주소
- RCA 체인 및 LLM 결과
- 성공 또는 오류 상태

API 키와 토큰 값은 기록하지 않습니다. 실패한 실행도 오류 원인과 함께 JSON으로
남기므로, 성공 결과만 선택적으로 모으는 오류를 방지할 수 있습니다.

## 권장 검증 순서

1. 정상 상태에서 `dfs_only`를 실행해 baseline을 확인합니다.
2. 복구 가능한 단일 장애를 주입합니다.
3. 같은 Git commit에서 `dfs_only`를 다시 실행합니다.
4. JSON의 최상위 RCA와 Ground Truth를 비교합니다.
5. 장애를 복구하고 baseline으로 돌아왔는지 확인합니다.
6. 단일 시나리오가 안정되면 `all` 모드와 반복 실험으로 확장합니다.

Pod 배포 여부는 엔진 정답률 검증 이후 결정합니다. Pod에 배포하는 것 자체는
RCA 알고리즘의 정확도를 높이거나 검증하지 않습니다.
