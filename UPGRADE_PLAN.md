# KubeIn-T 후속 연구 업그레이드 계획표

기준일: 2026-07-14

이 문서는 KubeIn을 단순 기능 추가가 아니라 **시간축과 다중 관측 근거를 사용하는 설명 가능한 RCA 시스템**으로 확장하기 위한 실행 계획이다. 가칭은 `KubeIn-T`이며, 이름과 논문 주장은 실험 결과가 나온 뒤 확정한다.

## 1. 검증할 연구 질문

- **RQ1 — 시간 정보:** 정적 단일 snapshot보다 장애 전·주입·전파·복구 구간을 함께 사용하면 Root Cause 순위와 탐지 지연이 개선되는가?
- **RQ2 — 다중 근거:** 의존성 그래프에 Kubernetes Events와 Prometheus 이상 징후를 구조화해 결합하면 그래프 단독 분석보다 정확도와 Blast Radius 품질이 개선되는가?
- **RQ3 — 신뢰도:** 근거의 일치도와 관측 완전성을 이용해 오답 가능성이 큰 결과를 식별하고, 필요할 때 답변을 보류할 수 있는가?
- **RQ4 — 비용:** 위 개선이 실제 운영에 사용할 수 있는 시간·메모리·API 호출 오버헤드 안에서 가능한가?

위 질문은 결론이 아니라 검증 대상이다. 성능 향상이 없으면 그 결과도 그대로 보고하고, 사후적으로 Ground Truth나 임계값을 바꾸어 결론을 맞추지 않는다.

## 2. 역할과 승인 원칙

| 영역 | 연구 책임자(사용자) | Codex |
|---|---|---|
| 연구 문제와 핵심 주장 | 최종 결정 및 이유 설명 | 후보·반례·관련 위험 제시 |
| Ground Truth | 최종 검토·승인 | 형식 검사와 불일치 탐지 |
| 알고리즘 설계 | 선택지 중 결정 | 구현안·장단점·검증법 제안 |
| 코드와 실험 자동화 | 결과 확인 및 재현 | 구현, 테스트, 실행 도구 작성 |
| 결과 해석 | 1차 해석과 주장 범위 결정 | 통계 분석과 악의적 리뷰 수행 |
| 논문 원고 | 모든 문장을 이해하고 최종 승인 | 구조·초안·교정 보조 |

연구 방향, 점수식, Ground Truth, 논문 주장처럼 결과를 바꾸는 결정은 사용자 승인 없이 확정하지 않는다. 중요한 결정은 아래 의사결정 기록에 남긴다.

## 3. 현재 기준선

| 항목 | 상태 | 비고 |
|---|---:|---|
| 정리 전 상태 백업 | 완료 | 저장소 외부 archive이며 환경변수·자격증명이 포함될 수 있으므로 공개 금지 |
| canonical 20-scenario manifest와 평가 runner | 완료 | 같은 evaluation snapshot을 세 mode가 공유 |
| miss와 infrastructure failure 분리 | 완료 | `valid/detected=false`와 `invalid` 구분 |
| 평가 coverage와 유효 정확도 분리 | 완료 | 제외·무효 trial은 정확도 분모에서 제외 |
| 실제 20개 scenario bundle | **차단** | 현재 checkout에 없으므로 동일 실험을 아직 재현할 수 없음 |
| Kubernetes Events | 부분 구현 | 현재는 최근 15분 Warning을 단순 시간·문자열 기준으로 묶으며 구조화된 RCA 점수에는 미사용 |
| Prometheus metrics | 부분 구현 | 현재는 고정 임계값 결과를 주로 LLM 문맥에만 제공하며 수집 실패와 정상값 구분이 약함 |
| typed dependency graph | 보완 필요 | Service selector 값을 Pod 이름에서 찾는 휴리스틱이 있어 실제 label key/value 및 endpoint 관계와 불일치 가능 |
| 시간 기반 evidence fusion | 미구현 | 정적 그래프 점수만 존재 |
| confidence/abstention | 미구현 | 심각도 점수와 예측 신뢰도가 분리되어 있지 않음 |

## 4. 마스터 일정

기간은 집중 작업일 기준 예상치다. 시나리오 번들 확보와 실제 클러스터 가용 시간에 따라 달라질 수 있다.

| 단계 | 예상 | 상태 | 핵심 작업 | 산출물 | 다음 단계 통과 조건 |
|---|---:|---:|---|---|---|
| **P0. 기준선 동결** | 1–2일 | 차단 | 논문에 사용한 scenario bundle 위치·버전·SHA-256 확인, Kubernetes/Chaos Mesh/Sock Shop/Prometheus 버전 기록, Event 보존기간·Prometheus scrape/retention·exporter·RBAC·clock skew preflight, 단일 cycle 재현 | bundle checksum, 환경 manifest, preflight·재현 로그 | **G0:** 필수 관측원이 검증되고 pre-check와 1 cycle이 새 오류 없이 완주 |
| **P1. 연구 명세 동결** | 1–2일 | 대기 | RQ1–RQ4, 귀무가설, 비교 baseline, 허용할 주장과 금지할 주장 확정, 최신 관련 연구와 claim matrix 대조 | 1쪽 연구 명세, claim matrix, 결정 기록 | **G1:** 사용자가 각 가설과 선택 이유를 설명하고 승인 |
| **P2. typed graph 정합성** | 2–4일 | 대기 | Pod label 보존, Service selector의 정확한 key/value match, ownerReference·EndpointSlice/Endpoints·Ingress·PVC/PV·ConfigMap/Secret 엣지의 방향과 provenance 검증 | versioned graph schema, topology fixtures | **G2:** fixture의 예상 node/typed edge 집합과 100% 일치하고 이름 부분문자열 연결 제거 |
| **P3. 시간축 데이터 계약** | 2–4일 | 대기 | `pre_fault → injected → propagating → recovering` 구간과 `t_inject_start/end` 기록, Prometheus scrape 간격보다 충분한 고정 관측 horizon 정의, 모든 관측에 공통 시계·run/scenario ID·수집 상태 부여 | versioned temporal evidence schema | **G3:** 저장 fixture를 재생하면 동일 evidence가 생성 |
| **P4. 근거 정규화** | 3–5일 | 대기 | Event의 최초·최근 발생 시각과 대상 UID 정규화, metric의 baseline 대비 변화량·onset 추출, graph node ID와 연결 | Event/metric evidence extractor, fixture tests | **G4:** 누락·지연·중복 이벤트와 NaN/빈 metric을 명시적으로 처리 |
| **P5. 설명 가능한 fusion v1** | 3–5일 | 대기 | graph·event·metric 성분을 별도로 계산, 점수 분해 제공, 관측 완전성/근거 일치도 기반 confidence와 abstention 정의 | fusion engine, score breakdown | **G5:** 단위 테스트와 ablation에서 각 성분의 효과를 독립 확인 |
| **P6. 기존 RCA/API 통합** | 2–4일 | 대기 | 기존 static mode 보존, 새 temporal mode 추가, Resource/Graph/Event/Metric과 source status를 하나의 불변 Observation Bundle로 동결·hash, 하위 호환 응답 유지 | additive API, bundle serializer/replay, regression tests | **G6:** 기존 baseline 결과 불변, 모든 mode의 bundle hash 일치, replay 결과 결정적 |
| **P7. Ground Truth 확장** | 2–4일 | 대기 | root cause, affected resources, 예상 관측 modality, 주입 시점/전파 구간을 결과를 보기 전에 주석 | versioned GT와 schema validator | **G7:** 모든 정량 시나리오를 사용자가 검토하고 GT 동결 |
| **P8. 파일럿** | 1–2일 | 대기 | 서로 다른 장애 유형 4–6개로 수집률·ID mapping·시간 정렬·복구 확인, 버그 수정 후 임계값 동결 | pilot report, frozen config | **G8:** 유효 trial coverage와 evidence 수집률이 사전 기준 충족 |
| **P9. 본 실험** | 2–5일+ | 대기 | 같은 trial을 모든 ablation이 공유하는 paired design, seed·순서·실패 원인 보존, 반복 횟수는 파일럿 변동성으로 결정 | 원본 JSON, run manifest, 무결성 검사 결과 | **G9:** 계획한 유효 표본과 coverage 확보, 사후 설정 변경 없음 |
| **P10. 분석·반증** | 2–3일 | 대기 | 평균뿐 아니라 95% CI·paired effect·실패 사례·risk-coverage·오버헤드 분석, 반대 가설 검토 | 표·그림·failure taxonomy | **G10:** 모든 주장이 재실행 가능한 결과 파일에 연결 |
| **P11. 8쪽 원고** | 3–5일 | 대기 | 기여점 2–3개로 제한, 설계·ablation·실패 사례 중심 작성, 위협요인과 AI 보조 범위 명시 | 제출용 원고와 artifact 안내 | **G11:** 사용자가 전체 방법과 결과를 구두로 방어 가능 |

P1의 연구 명세와 관련 연구 정리는 P0의 번들 탐색과 병행할 수 있다. 그러나 **P8 이후 실제 성능 실험은 G0와 G7을 모두 통과한 뒤에만 시작**한다.

## 5. 제안하는 시스템 구조

```text
동일 trial 시간창
  ├─ 정적 리소스/의존성 그래프 ── graph evidence
  ├─ Kubernetes Events ───────── event evidence + onset
  └─ Prometheus 시계열 ───────── metric evidence + change point
                    │
                    ▼
          resource ID/시간축 정규화
                    │
                    ▼
       설명 가능한 evidence fusion
        ├─ root-cause ranking
        ├─ blast-radius prediction
        ├─ score breakdown
        └─ confidence / abstain
```

중요한 제한은 다음과 같다.

- 시간 순서와 그래프 연결은 인과 **후보 근거**로만 사용한다. 통제된 개입이나 반사실 검증이 없으면 `causal` 또는 인과관계를 입증했다는 주장을 하지 않는다.
- LLM은 설명 생성에 사용할 수 있지만 정량 RCA 점수의 숨은 판정자로 넣지 않는다.
- `수집 실패`, `해당 신호 없음`, `정상 관측`은 서로 다른 상태로 저장한다.
- Pod 재생성으로 인한 이름 충돌을 막기 위해 가능한 곳에서는 kind/namespace/name뿐 아니라 UID와 resourceVersion을 함께 보존한다.
- runner는 `error_node_count > 0`을 시간축 캡처 시작 조건으로 사용하지 않는다. 주입 시작·종료 시각과 사전에 고정한 관측 horizon으로 bundle을 한 번 캡처한다.
- severity와 confidence를 분리한다. 심각한 장애라고 해서 진단 신뢰도가 높은 것은 아니다.
- 데이터가 작은 첫 버전은 학습형 black-box보다 고정된 설명 가능 점수식과 ablation을 우선한다.

## 6. 필수 ablation과 지표

### 비교 설정

| ID | 설정 | 검증 목적 |
|---|---|---|
| A0 | 기존 Static KubeIn | legacy 기준선 |
| A1 | Typed Static KubeIn | 정확한 topology 자체의 효과 |
| A2 | A1 + Temporal state | 시간축 자체의 효과 |
| A3 | A2 + Kubernetes Events | Event 근거의 추가 효과 |
| A4 | A2 + Prometheus Metrics | Metric 근거의 추가 효과 |
| A5 | A2 + Events + Metrics | 전체 structured fusion 효과 |
| A6 | A5에서 confidence/abstention 활성화 | 안전한 보류의 효과 |

필요하면 LLM 설명은 별도 축으로 평가한다. LLM 유무가 구조화 RCA 성능 비교를 흐리지 않도록 A0–A6의 순위 계산은 동일한 비-LLM 경로를 사용하고, 한 trial의 모든 설정은 동일한 Observation Bundle hash를 공유한다.

### 보고 지표

| 목표 | 지표 |
|---|---|
| Root Cause 순위 | Hit@1, Hit@3, MRR |
| 전파 범위 | Blast Radius Precision, Recall, F1 |
| 시간성 | detection latency, onset error |
| 신뢰도 | Brier score, ECE, risk-coverage/abstention curve |
| 운영 비용 | 분석 지연, 수집 API 수, peak memory, snapshot 크기 |
| 실험 건전성 | valid coverage, modality availability, recovery failure rate |

정확도는 유효 trial만 분모로 계산하되 coverage를 항상 함께 보고한다. 5-cycle은 기존 결과와의 비교를 위한 최소 기준으로만 보고, 최종 반복 횟수는 파일럿 분산과 실행 비용을 근거로 동결한다.

## 7. 의사결정 기록

| ID | 결정할 내용 | 권고 초안 | 최종 결정 | 책임자 | 상태 |
|---|---|---|---|---|---|
| D1 | 후속 논문의 핵심 기여 | 시간축 + 구조화 다중 근거 RCA | 미정 | 사용자 | 대기 |
| D2 | fusion 방식 | 설명 가능한 고정 점수식으로 시작 | 미정 | 사용자 | 대기 |
| D3 | confidence의 의미 | 정답 확률이 아니라 근거 기반 신뢰도로 먼저 정의 | 미정 | 사용자 | 대기 |
| D4 | 본 실험 반복 수 | 파일럿 후 동결, 기존 비교용 최소 5회 | 미정 | 사용자 | 대기 |
| D5 | 논문 제목과 성능 주장 | 본 실험 전에는 확정하지 않음 | 미정 | 사용자 | 대기 |

결정을 바꿀 때는 날짜, 변경 이유, 변경 전에 본 결과의 범위를 함께 기록한다.

## 8. 중단·피벗 기준

- 동일 scenario bundle과 환경을 확인하지 못하면 새 성능 향상 주장을 만들지 않는다.
- Event/metric의 resource mapping 또는 시간 동기화가 안정적이지 않으면 해당 modality를 `unavailable`로 보고하고 fusion 입력에서 제외한다.
- 파일럿 결과를 본 뒤 GT를 변경해야 한다면 GT 버전을 올리고, 해당 결과는 탐색 실험으로만 분류한 뒤 본 실험을 처음부터 다시 수행한다.
- 신뢰도 calibration용 표본이 부족하면 “calibrated probability”라고 주장하지 않고 evidence completeness 점수로 한정한다.
- A4가 A0보다 개선되지 않으면 성능 향상을 주장하지 않는다. 대신 어떤 장애 유형에서 어떤 근거가 실패했는지 분석하고, 필요하면 범위를 축소하거나 연구 질문을 수정한다.
- 통제된 개입·반사실 실험을 하지 않으면 논문 범위를 temporal/evidence-aware RCA로 제한하고 causal RCA라고 명명하지 않는다.
- LLM-as-a-Judge 점수만으로 시스템 정확도나 우월성을 주장하지 않는다.

## 9. 바로 다음 작업

1. 논문 실험에 사용한 20개 scenario bundle의 원본 위치를 찾아 버전과 checksum을 확정한다.
2. RQ1–RQ4 중 실제 후속 논문에서 방어할 핵심 질문을 2개 이하로 좁혀 D1을 승인한다.
3. 구현 전에 topology fixture와 temporal evidence schema를 먼저 만들고, 저장된 fixture만으로 P2–P5를 검증한다.
4. 기존 static 결과를 절대 덮어쓰지 않는 별도 mode로 최소 기능을 통합한다.
