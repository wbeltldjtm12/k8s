# KubeIn

Kubernetes 리소스 의존성 그래프를 기반으로 장애 원인과 전파 범위를 분석하고,
RAG/LLM 설명을 제공하는 FastAPI + Streamlit 애플리케이션입니다.

## 디렉터리

- `backend/`: API, Kubernetes 수집기, RCA 그래프, ChromaDB/LLM
- `frontend/`: Streamlit 대시보드
- `cluster-port/`: 현재 배포 설정과 canonical 20-scenario 평가 runner
- `scripts/`: 과거 single-node 경로를 유지하는 호환 wrapper와 legacy 연구 스크립트
- `eval/`: canonical `scenarios.txt`, Ground Truth와 정량 평가 도구
- `judge/`: 다중 LLM 판정 스크립트와 보존된 5-cycle 판정 결과

`backend/chroma_db/`, `eval/ground_truth.json`, `judge/judge_results_5cycle.json`은
현재 저장소에서 다시 만들 원천 데이터가 완전하지 않으므로 생성 캐시로 보고 삭제하면 안 됩니다.

## 로컬 실행

```bash
python -m pip install -r backend/requirements.txt -r frontend/requirements.txt
python run.py
```

## 평가 실행

클러스터 배포와 평가 절차는 `cluster-port/README.md`를 기준으로 합니다.
현재 runner는 평가 snapshot을 한 번 생성하고 같은 `snapshot_id`로 세 분석 모드를 호출합니다.

20개 장애 scenario bundle은 이 저장소에 포함되어 있지 않습니다. 따라서 현재 checkout만으로는
end-to-end 실험을 재현할 수 없으며, 논문에 사용한 동일 bundle의 위치·버전·checksum을 별도로
확정해야 합니다. 필요한 구조와 실행 profile은 `cluster-port/README.md`에 정리되어 있습니다.

## 후속 연구

시간축·Kubernetes Events·Prometheus 근거를 결합하는 후속 확장 계획과 단계별 승인 조건은
[UPGRADE_PLAN.md](UPGRADE_PLAN.md)에 정리되어 있습니다.
