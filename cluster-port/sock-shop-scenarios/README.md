# KubeIn 논문 재현용 Sock Shop 장애 시나리오

이 디렉터리는 KubeIn 원 논문의 20개 장애 시나리오를 현재 `sock-shop`
네임스페이스에서 다시 실행할 수 있게 정리한 bundle이다.

원본 HWP에는 시나리오의 **이름, 대상 서비스, 유형, 관측 범위**가 기록되어
있지만, 당시 실행한 개별 YAML과 쉘 명령 전문은 포함되어 있지 않다. 따라서 이
bundle은 다음 두 자료를 기준으로 재구성했다.

- 원 논문 4.2절 및 5.1절: 20개 유형, Chaos Mesh + `kubectl` 병행, API
  snapshot으로 관측 가능한/불가능한 분류
- `eval/scenarios.txt`, `eval/ground_truth.json`: 시나리오 ID, 대상 리소스,
  기대 root cause와 영향 범위

즉 **논문의 실험 설계를 재현하는 구현**이지, 유실된 당시 스크립트의 바이트 단위
복원본은 아니다. 각 스크립트의 주입 방식과 차이는 아래 표에 명시했다.

## 실행 전제

```bash
cd ~/kubein
kubectl get pods -n sock-shop

# Chaos Mesh-based scenarios (8) require the CRDs below
kubectl get crd podchaos.chaos-mesh.org
```

`01`~`03`, `11`~`15`, `17`은 Chaos Mesh CRD가 필요하다. 나머지는 `kubectl`
만으로 동작한다. 실제 실험은 단일 케이스부터 시작해야 한다.

```bash
export SCENARIO_DIR="$PWD/cluster-port/sock-shop-scenarios"
export GROUND_TRUTH_FILE="$PWD/eval/ground_truth.json"

# 예: 04-bad-image를 수동으로 주입/복구
bash "$SCENARIO_DIR/04-bad-image/inject.sh"
# KubeIn 엔진 분석
bash "$SCENARIO_DIR/04-bad-image/recover.sh"
```

Chaos Mesh YAML 케이스는 다음처럼 주입/삭제한다.

```bash
kubectl apply -f "$SCENARIO_DIR/01-pod-kill/fault.yaml"
kubectl delete -f "$SCENARIO_DIR/01-pod-kill/fault.yaml" --ignore-not-found
```

전체 runner는 현재 이 bundle을 기본 경로로 사용한다.

```bash
bash cluster-port/scripts/04-ablation-eval-cluster.sh
```

주입 완료 뒤 관측 전 대기시간은 bundle 루트의
`scenario-metadata.json`에서 관리한다. 기본값은 5초이고 필요한 시나리오만
`observation_settle_seconds`를 덮어쓴다. 복구 직후 늦게 나타나는 상태 전이를
기다려야 하는 경우에는 기본값 0초인 `recovery_settle_seconds`를 사용한다.
runner와 pre-check는
`eval/validate-scenario-metadata.py`로 ID·키·타입·0~20초 범위를 엄격히
검증하며 이 JSON을 shell source로 읽지 않는다. resolve된 값은 실행 로그와
`observation-evidence.txt`에 기록된다.

## 시나리오 목록

| ID | 논문 유형 | 대상 | 주입 방식 | API snapshot 정량 대상 |
|---|---|---|---|---|
| 01 | Pod kill | carts-db | PodChaos | 아니오 |
| 02 | Pod failure | catalogue | PodChaos `pod-failure` | 예 (`Deployment/NoAvailableReplicas`) |
| 03 | Container kill | orders | PodChaos | 아니오 |
| 04 | Bad image | catalogue | 잘못된 이미지로 rollout | 예 |
| 05 | OOMKill | orders | 낮은 memory limit rollout | 예 |
| 06 | Readiness probe failure | payment | 실패하는 readiness probe | 예 |
| 07 | Liveness probe failure | catalogue | 실패하는 liveness probe | 타이밍 의존 |
| 08 | Secret delete | catalogue-db | Secret 참조 후 삭제 | 예 |
| 09 | Bad ConfigMap | front-end | 잘못된 `NODE_OPTIONS` ConfigMap | 예 |
| 10 | Missing PVC | orders-db | 존재하지 않는 PVC mount | 예 |
| 11 | DNS error | shipping | DNSChaos | 아니오 |
| 12 | Network delay | payment | NetworkChaos | 아니오 |
| 13 | Network loss | front-end | NetworkChaos | 아니오 |
| 14 | CPU stress | shipping | StressChaos | 아니오 |
| 15 | IO delay | carts-db | IOChaos | 아니오 |
| 16 | Selector mismatch | user | Service selector 변경 | 예 |
| 17 | Memory stress | shipping | StressChaos | 아니오 |
| 18 | Cascade carts | carts-db → carts | 순차 Pod 삭제 | 아니오 |
| 19 | Cascade secret | catalogue-db | 공유 Secret 삭제 | 예 |
| 20 | Multi-fault | payment + user | readiness + selector | 예 |

## 안전/복구 규칙

- 수동 케이스의 원래 replica 수와 Service selector는 `SCENARIO_STATE_DIR`에
  저장한다. runner가 실행하면 결과 폴더의 `state/`를 사용한다.
- 수동으로 실행하면 각 시나리오의 `.state/`를 사용한다. 이 경로는 git에서 제외된다.
- Deployment 변경 케이스는 `kubectl rollout undo`로 이전 revision을 복구한다.
- `08`, `19`는 실험용 Secret만 만들고 삭제한다. 기존 Secret은 건드리지 않는다.
- `09`는 실험용 ConfigMap만 만들고 삭제한다.
- `10`은 존재하지 않는 PVC 이름을 **참조만** 하며, 실제 PV/PVC를 삭제하지 않는다.
- `02`의 이전 scale-to-zero 스크립트는 `.disabled` 파일로 보존한다. Kubernetes에서
  `replicas=0`은 정상적인 workload suspend이므로 canonical runner는 실제
  PodChaos `pod-failure` manifest만 주입한다. 복구 스크립트는 Chaos 삭제 후
  catalogue Pod를 한 번 교체해 `CrashLoopBackOff` restart residue를 제거한다.

## 관측상 주의할 점

원 논문과 동일하게 `01`, `03`, `11`~`15`, `17`, `18`은 Kubernetes 자가 복구나
네트워크/메트릭 계층 장애라 API snapshot에서 안정적으로 남지 않을 수 있다. 이들은
"실험 실패"가 아니라 KubeIn의 API-only 관측 한계를 확인하는 비교군이다.

`02-pod-failure`는 Chaos Mesh가 catalogue Pod를 일정 시간 unavailable로 유지한다.
catalogue의 readiness/liveness probe가 실패 상태를 Kubernetes API에 노출하므로
scale-to-zero와 달리 실제 장애 snapshot을 반복 검증할 수 있다. 3회의 완전한
3-mode targeted 실행에서 `Deployment/catalogue/NoAvailableReplicas`가 모두
Top-1으로 재현됐다. `07-liveness-probe`는
빠른 재시작 때문에 snapshot 시점에 정상으로 보일 수 있는 타이밍 의존 케이스다.

`12-network-delay`의 관측 대기는 의도적으로 5초다. 이를 약 10초 이상으로
늘리면 1초 network delay가 payment readiness/liveness probe의 3회 실패를
만들고, 저수준 NetworkChaos가 아니라 probe 유발 재시작이라는 2차 장애가
snapshot에 섞일 수 있다. 현재 API-only 비정량 대조군 의미를 유지하기 위해
관측값은 5초로 유지한다. 대신 Chaos 삭제 직후 발생할 수 있는 늦은 container
restart를 안정화 검사가 놓치지 않도록 recovery settle 12초를 명시한다.
