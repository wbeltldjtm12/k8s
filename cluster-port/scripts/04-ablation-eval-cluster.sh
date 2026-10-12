#!/usr/bin/env bash
# 04-ablation-eval-cluster.sh
# KubeIn canonical ablation 평가 스크립트
#
#   - SCENARIO_DIR/API_BASE 환경변수 지원
#   - 한 번 캡처한 evaluation snapshot을 3개 mode가 공유
#   - API/JSON/snapshot 검증 실패를 invalid 결과와 exit code로 전파

set -uo pipefail

RUN_LOCK_FILE="${RUN_LOCK_FILE:-/tmp/kubein-ablation.lock}"
if [ "${KUBEIN_ABLATION_LOCK_HELD:-0}" != "1" ]; then
  command -v flock >/dev/null 2>&1 || {
    echo "[오류] 동시 실행 방지에 필요한 flock을 찾을 수 없습니다." >&2
    exit 2
  }
  exec 9>"$RUN_LOCK_FILE"
  if ! flock -n 9; then
    echo "[오류] 다른 KUBEIN ablation 실행이 이미 진행 중입니다: $RUN_LOCK_FILE" >&2
    exit 75
  fi
fi

# ── 환경변수 설정 (오버라이드 가능) ───────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCENARIO_DIR="${SCENARIO_DIR:-$REPO_ROOT/cluster-port/sock-shop-scenarios}"
API_BASE="${API_BASE:-http://localhost:8000}"
PROMETHEUS_URL="${PROMETHEUS_URL:-}"
RESULTS_ROOT="${SCENARIO_DIR}/results"
CACHE_API="$API_BASE/api/cache/invalidate"
EVAL_API="$API_BASE/api/evaluate"
SNAPSHOT_API="$API_BASE/api/evaluate/snapshots"
NS="sock-shop"
EVAL_TIMEOUT_SECONDS="${EVAL_TIMEOUT_SECONDS:-960}"
LLM_MODE_INTERVAL_SECONDS="${LLM_MODE_INTERVAL_SECONDS:-0}"
SNAPSHOT_ACQUIRE_MARGIN_SECONDS="${SNAPSHOT_ACQUIRE_MARGIN_SECONDS:-30}"
DFS_MODE_PLANNED_SECONDS="${DFS_MODE_PLANNED_SECONDS:-120}"
RUN_FAILURES=0
ACTIVE_SNAPSHOT_ID=""
ACTIVE_SNAPSHOT_DEADLINE=0
ACTIVE_SCENARIO=""
CLEANUP_IN_PROGRESS=0
LAST_LLM_MODE_EPOCH=0
declare -A RECOVERY_ATTEMPTS=()
declare -A SCENARIO_SETTLE_SECONDS=()
declare -A SCENARIO_RECOVERY_SETTLE_SECONDS=()
GROUND_TRUTH_FILE="${GROUND_TRUTH_FILE:-}"
EXCLUDED_SCENARIOS_CSV="${EXCLUDED_SCENARIOS_CSV-}"
SCENARIO_MANIFEST="${SCENARIO_MANIFEST:-$REPO_ROOT/eval/scenarios.txt}"
SCENARIO_METADATA_FILE="${SCENARIO_METADATA_FILE:-$SCENARIO_DIR/scenario-metadata.json}"
SCENARIO_METADATA_VALIDATOR="${SCENARIO_METADATA_VALIDATOR:-$REPO_ROOT/eval/validate-scenario-metadata.py}"
CHAOS_INJECT_TIMEOUT_SECONDS="${CHAOS_INJECT_TIMEOUT_SECONDS:-30}"
CLUSTER_STABILITY_TIMEOUT_SECONDS="${CLUSTER_STABILITY_TIMEOUT_SECONDS:-300}"
CLUSTER_STABILITY_POLL_INTERVAL_SECONDS="${CLUSTER_STABILITY_POLL_INTERVAL_SECONDS:-2}"
CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS="${CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS:-5}"
CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV="${CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV:-carts,carts-db,catalogue,catalogue-db,front-end,orders,orders-db,payment,queue-master,rabbitmq,session-db,shipping,user,user-db}"

mkdir -p "$RESULTS_ROOT" || exit 2
RESULT_DIR="$(mktemp -d "$RESULTS_ROOT/ablation_$(date +%Y%m%d_%H%M%S)_XXXXXX")" || exit 2
LATEST_POINTER="$RESULTS_ROOT/latest_ablation_dir.txt"

if [ ! -f "$SCENARIO_MANIFEST" ]; then
  echo "[오류] scenario manifest를 찾을 수 없음: $SCENARIO_MANIFEST" >&2
  exit 2
fi
mapfile -t SCENARIOS < <(
  sed -e 's/\r$//' -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$SCENARIO_MANIFEST"
)
if [ "${#SCENARIOS[@]}" -ne 20 ] \
  || [ "$(printf '%s\n' "${SCENARIOS[@]}" | sort -u | wc -l | tr -d ' ')" -ne 20 ]; then
  echo "[오류] scenario manifest는 고유한 20개 항목이어야 함: $SCENARIO_MANIFEST" >&2
  exit 2
fi
if [ ! -f "$SCENARIO_METADATA_VALIDATOR" ]; then
  echo "[오류] scenario metadata validator를 찾을 수 없음: $SCENARIO_METADATA_VALIDATOR" >&2
  exit 2
fi
if [ ! -f "$SCENARIO_METADATA_FILE" ]; then
  echo "[오류] scenario metadata를 찾을 수 없음: $SCENARIO_METADATA_FILE" >&2
  exit 2
fi
if ! SCENARIO_SETTLE_ROWS="$(
  python3 "$SCENARIO_METADATA_VALIDATOR" \
    --metadata "$SCENARIO_METADATA_FILE" \
    --manifest "$SCENARIO_MANIFEST"
)"; then
  echo "[오류] scenario metadata 검증 실패: $SCENARIO_METADATA_FILE" >&2
  exit 2
fi
while IFS=$'\t' read -r scenario_name settle_seconds recovery_settle_seconds; do
  if [ -z "$scenario_name" ] \
    || ! [[ "$settle_seconds" =~ ^[0-9]+$ ]] \
    || ! [[ "$recovery_settle_seconds" =~ ^[0-9]+$ ]]; then
    echo "[오류] scenario metadata validator 출력이 올바르지 않음" >&2
    exit 2
  fi
  SCENARIO_SETTLE_SECONDS["$scenario_name"]="$settle_seconds"
  SCENARIO_RECOVERY_SETTLE_SECONDS["$scenario_name"]="$recovery_settle_seconds"
done <<< "$SCENARIO_SETTLE_ROWS"
if [ "${#SCENARIO_SETTLE_SECONDS[@]}" -ne "${#SCENARIOS[@]}" ] \
  || [ "${#SCENARIO_RECOVERY_SETTLE_SECONDS[@]}" -ne "${#SCENARIOS[@]}" ]; then
  echo "[오류] scenario metadata가 전체 manifest를 resolve하지 못함" >&2
  exit 2
fi
IFS=',' read -r -a EXCLUDED_SCENARIOS <<< "$EXCLUDED_SCENARIOS_CSV"
EVAL_MODES_CSV="${EVAL_MODES_CSV:-hybrid,dfs_only,llm_only}"
IFS=',' read -r -a MODES <<< "$EVAL_MODES_CSV"
if [ "${#MODES[@]}" -eq 0 ]; then
  echo "[오류] EVAL_MODES_CSV는 하나 이상의 mode를 지정해야 합니다." >&2
  exit 2
fi
if ! [[ "$LLM_MODE_INTERVAL_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "[오류] LLM_MODE_INTERVAL_SECONDS는 0 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$EVAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] EVAL_TIMEOUT_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$SNAPSHOT_ACQUIRE_MARGIN_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] SNAPSHOT_ACQUIRE_MARGIN_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$DFS_MODE_PLANNED_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] DFS_MODE_PLANNED_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$CHAOS_INJECT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] CHAOS_INJECT_TIMEOUT_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$CLUSTER_STABILITY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] CLUSTER_STABILITY_TIMEOUT_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$CLUSTER_STABILITY_POLL_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] CLUSTER_STABILITY_POLL_INTERVAL_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if ! [[ "$CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS는 1 이상의 정수여야 합니다." >&2
  exit 2
fi
if [ -z "$CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV" ]; then
  echo "[오류] CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV는 비어 있을 수 없습니다." >&2
  exit 2
fi
for mode in "${MODES[@]}"; do
  case "$mode" in
    hybrid|dfs_only|llm_only) ;;
    *)
      echo "[오류] 지원하지 않는 평가 mode: $mode" >&2
      exit 2
      ;;
  esac
done

if [ -z "$GROUND_TRUTH_FILE" ]; then
  for candidate in \
    "$SCENARIO_DIR/eval/ground_truth.json" \
    "$SCENARIO_DIR/ground_truth.json" \
    "$SCENARIO_DIR/ground_truth.json.txt" \
    "$REPO_ROOT/eval/ground_truth.json"; do
    if [ -f "$candidate" ]; then
      GROUND_TRUTH_FILE="$candidate"
      break
    fi
  done
fi
if [ -z "$GROUND_TRUTH_FILE" ] || [ ! -f "$GROUND_TRUTH_FILE" ]; then
  echo "[오류] ground_truth.json을 찾을 수 없습니다." >&2
  exit 2
fi
if ! GT_DETECTABLE_TEXT="$(python3 - "$GROUND_TRUTH_FILE" "${SCENARIOS[@]}" <<'PY'
import json
import sys

path, *active = sys.argv[1:]
with open(path, "r", encoding="utf-8") as stream:
    data = json.load(stream)
if not isinstance(data, dict) or any(name not in data for name in active):
    raise SystemExit("invalid or incomplete ground truth")
for name in active:
    value = data[name]
    if not isinstance(value, dict) or not isinstance(value.get("detectable"), bool):
        raise SystemExit(f"invalid ground truth entry: {name}")
    if value["detectable"]:
        print(name)
PY
)"; then
  echo "[오류] Ground Truth 스키마 검증 실패: $GROUND_TRUTH_FILE" >&2
  exit 2
fi
mapfile -t DETECTABLE_SCENARIOS <<< "$GT_DETECTABLE_TEXT"

echo "=========================================="
echo " KubeIn Ablation Study [클러스터 모드]"
echo " SCENARIO_DIR: $SCENARIO_DIR"
echo " METADATA    : $SCENARIO_METADATA_FILE"
echo " API_BASE    : $API_BASE"
echo " 결과 경로   : $RESULT_DIR"
echo "=========================================="

log() { echo "$*"; }

wait_recovery_settle() {
  local scen="$1"
  local settle_seconds="${SCENARIO_RECOVERY_SETTLE_SECONDS[$scen]}"
  [ "$settle_seconds" -eq 0 ] && return 0
  log "  [대기] recovery settle ${settle_seconds}초..."
  sleep "$settle_seconds"
}

release_active_snapshot() {
  local snapshot_id="${ACTIVE_SNAPSHOT_ID:-}"
  [ -z "$snapshot_id" ] && return 0
  if curl -fsS --connect-timeout 10 --max-time 30 \
    -X DELETE "$SNAPSHOT_API/$snapshot_id" >/dev/null; then
    if [ "$ACTIVE_SNAPSHOT_ID" = "$snapshot_id" ]; then
      ACTIVE_SNAPSHOT_ID=""
      ACTIVE_SNAPSHOT_DEADLINE=0
    fi
    return 0
  fi
  return 1
}

cleanup_on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [ "$CLEANUP_IN_PROGRESS" -eq 1 ]; then
    exit "$rc"
  fi
  CLEANUP_IN_PROGRESS=1
  release_active_snapshot || true
  if [ -n "${ACTIVE_SCENARIO:-}" ]; then
    log "[종료 복구] 활성 시나리오 자동 복구: $ACTIVE_SCENARIO"
    recover_scenario "$ACTIVE_SCENARIO" || true
    wait_recovery_settle "$ACTIVE_SCENARIO" || true
    wait_cluster_stable || true
    wait_baseline_zero || true
    ACTIVE_SCENARIO=""
  fi
  exit "$rc"
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

invalidate_cache() {
  curl -fsS --connect-timeout 10 --max-time 30 -X POST "$CACHE_API" >/dev/null
}

get_eval_json() {
  curl -fsS --connect-timeout 10 --max-time "$EVAL_TIMEOUT_SECONDS" \
    --get --data-urlencode "mode=${1:-dfs_only}" "$EVAL_API"
}

get_error_count() {
  local response count
  response="$(get_eval_json dfs_only)" || return 1
  count="$(printf '%s' "$response" | python3 -c 'import sys,json
d=json.load(sys.stdin)
if d.get("status") != "success": raise ValueError("API status is not success")
value=d.get("error_node_count")
if not isinstance(value, int) or isinstance(value, bool): raise TypeError("invalid error_node_count")
print(value)')" || return 1
  printf '%s\n' "$count"
}

get_top_chain_fields() {
  get_eval_json dfs_only | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    chains=d.get("data",{}).get("chains",[])
    if not chains:
        print(""); print(""); print(""); print("")
    else:
        c=chains[0]
        print(c.get("root_cause_kind",""))
        print(c.get("root_cause",""))
        print(c.get("root_cause_reason",""))
        print(c.get("score",""))
except Exception:
    print(""); print(""); print(""); print("")'
}

is_in_list() {
  local target="$1"; shift
  for x in "$@"; do [ "$x" = "$target" ] && return 0; done
  return 1
}

is_gt_detectable() {
  is_in_list "$1" "${DETECTABLE_SCENARIOS[@]}"
}

delete_zero_rs() {
  kubectl get rs -n "$NS" --no-headers 2>/dev/null \
    | awk '$2+0==0{print $1}' \
    | xargs -r kubectl delete rs -n "$NS" >/dev/null 2>&1 || true
}

cluster_kubectl_with_hard_timeout() {
  local timeout_seconds="$1"
  shift
  if ! command -v timeout >/dev/null 2>&1; then
    log "  [실패] kubectl 요청 제한에 필요한 timeout 명령을 찾을 수 없음"
    return 127
  fi
  # Exporting a mock keeps the behavior contract executable; production uses
  # the kubectl executable resolved by the child Bash process.
  if declare -F kubectl >/dev/null 2>&1; then
    export -f kubectl
  fi
  # This helper is used only for read-only current-state queries.  Kill the
  # timeout process group at the deadline so no descendant can retain the
  # command-substitution pipe after its parent shell exits.
  timeout --signal=KILL "${timeout_seconds}s" \
    bash -c 'kubectl "$@"' bash "$@"
}

wait_cluster_stable() {
  local timeout_seconds="${CLUSTER_STABILITY_TIMEOUT_SECONDS:-300}"
  local poll_interval="${CLUSTER_STABILITY_POLL_INTERVAL_SECONDS:-2}"
  local request_timeout_cap="${CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS:-5}"
  local expected_csv="${CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV:-}"
  local deadline remaining request_timeout sleep_for payload state

  log "  [대기] deployment / 현재 pod 안정화 확인"
  deadline=$((SECONDS + timeout_seconds))
  state="아직 조회하지 못함"

  while true; do
    remaining=$((deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
      log "  [실패] 클러스터 안정화 제한 시간 초과: $state"
      return 1
    fi
    request_timeout="$request_timeout_cap"
    [ "$request_timeout" -le "$remaining" ] || request_timeout="$remaining"
    payload=""
    if payload="$(cluster_kubectl_with_hard_timeout "$request_timeout" \
      get deployment,pod -n "$NS" -o json \
      --request-timeout="${request_timeout}s" 2>/dev/null)"; then
      if state="$(python3 -c '
import json
import sys

expected = {name for name in sys.argv[1].split(",") if name}
try:
    document = json.load(sys.stdin)
except Exception as exc:
    print(f"invalid kubectl JSON: {type(exc).__name__}")
    raise SystemExit(1)

items = document.get("items")
if not isinstance(items, list):
    print("kubectl response has no item list")
    raise SystemExit(1)

deployments = [item for item in items if item.get("kind") == "Deployment"]
pods = [
    item
    for item in items
    if item.get("kind") == "Pod"
    and not item.get("metadata", {}).get("deletionTimestamp")
]
deployment_names = set(
    item.get("metadata", {}).get("name", "")
    for item in deployments
)
problems = []
missing = sorted(expected - deployment_names)
if missing:
    problems.append("missing_deployments=" + ",".join(missing))
if not deployments:
    problems.append("deployments=0")

desired_total = 0
for deployment in deployments:
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    name = metadata.get("name") or "<unnamed>"
    generation = metadata.get("generation")
    observed = status.get("observedGeneration", 0)
    desired = spec.get("replicas", 1)
    values = (
        generation,
        observed,
        desired,
        status.get("replicas", 0),
        status.get("updatedReplicas", 0),
        status.get("readyReplicas", 0),
        status.get("availableReplicas", 0),
        status.get("unavailableReplicas", 0),
    )
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        problems.append(f"deployment={name}:invalid_status")
        continue
    desired_total += desired
    if not (
        observed >= generation
        and values[3] == desired
        and values[4] == desired
        and values[5] == desired
        and values[6] == desired
        and values[7] == 0
    ):
        problems.append(
            f"deployment={name}:generation={generation}/{observed},"
            f"replicas={desired}/{values[3]}/{values[4]}/{values[5]}/{values[6]},"
            f"unavailable={values[7]}"
        )

if desired_total <= 0:
    problems.append(f"desired_replicas={desired_total}")
if len(pods) < desired_total:
    problems.append(f"current_pods={len(pods)}<desired_replicas={desired_total}")
if not pods:
    problems.append("current_pods=0")

for pod in pods:
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    name = metadata.get("name") or "<unnamed>"
    phase = status.get("phase", "<missing>")
    container_statuses = status.get("containerStatuses")
    ready_condition = any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in status.get("conditions", [])
        if isinstance(condition, dict)
    )
    containers_ready = (
        isinstance(container_statuses, list)
        and bool(container_statuses)
        and all(item.get("ready") is True for item in container_statuses)
    )
    if phase != "Running" or not ready_condition or not containers_ready:
        problems.append(
            f"pod={name}:phase={phase},"
            f"ready={ready_condition},containers_ready={containers_ready}"
        )

summary = (
    f"deployments={len(deployments)},desired_replicas={desired_total},"
    f"current_pods={len(pods)}"
)
if problems:
    print(summary + "; " + "; ".join(problems[:8]))
    raise SystemExit(1)
print(summary)
' "$expected_csv" <<< "$payload")"; then
        log "  [OK] 클러스터 안정: $state"
        return 0
      fi
    else
      state="kubectl 현재 상태 조회 실패/시간 초과"
    fi

    remaining=$((deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
      log "  [실패] 클러스터 안정화 제한 시간 초과: $state"
      return 1
    fi
    sleep_for="$poll_interval"
    [ "$sleep_for" -le "$remaining" ] || sleep_for="$remaining"
    sleep "$sleep_for"
  done
}

cleanup_residue_aggressive() {
  local kind name reason score
  mapfile -t FIELDS < <(get_top_chain_fields)
  kind="${FIELDS[0]:-}"; name="${FIELDS[1]:-}"
  reason="${FIELDS[2]:-}"; score="${FIELDS[3]:-}"

  log "  [잔여 분석] kind=${kind:-N/A} name=${name:-N/A} reason=${reason:-N/A}"

  if [ "$kind" = "Pod" ] && [ "$reason" = "HighRestartCount" ] && [ -n "$name" ]; then
    log "  [자가치유] HighRestartCount Pod 삭제: $name"
    kubectl delete pod "$name" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
    wait_cluster_stable || return 1
    invalidate_cache || true
    sleep 5
    return 0
  fi
  return 1
}

wait_baseline_zero() {
  local max=18 i cnt
  for i in $(seq 1 "$max"); do
    if ! invalidate_cache; then
      log "  [baseline $i/$max] 캐시 무효화 API 실패"
      sleep 3
      continue
    fi
    sleep 2
    if ! cnt="$(get_error_count)"; then
      log "  [baseline $i/$max] 평가 API/JSON 실패"
      sleep 3
      continue
    fi
    [ "$cnt" -eq 0 ] && log "  [OK] baseline=0" && return 0
    log "  [baseline $i/$max] error=$cnt"
    cleanup_residue_aggressive && sleep 3 && continue
    sleep 3
  done
  log "  [실패] baseline이 0 안 됨"
  return 1
}

verify_chaos_injected() {
  local fault_file="$1" outdir="$2"
  local attempt status_tmp
  status_tmp="$outdir/fault-status.json.tmp"

  for attempt in $(seq 1 "$CHAOS_INJECT_TIMEOUT_SECONDS"); do
    if kubectl get -f "$fault_file" -o json > "$status_tmp" 2>/dev/null \
      && python3 - "$status_tmp" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
conditions = {
    item.get("type"): item.get("status")
    for item in data.get("status", {}).get("conditions", [])
    if isinstance(item, dict)
}
desired = data.get("status", {}).get("experiment", {}).get("desiredPhase")
selected = conditions.get("Selected") == "True"
injected = conditions.get("AllInjected") == "True"
finished = conditions.get("AllRecovered") == "True" and desired == "Stop"
paused = conditions.get("Paused") == "True"
if not selected or paused or not (injected or finished):
    raise SystemExit(1)
PY
    then
      mv -f "$status_tmp" "$outdir/fault-status.json"
      return 0
    fi
    sleep 1
  done

  [ -f "$status_tmp" ] && mv -f "$status_tmp" "$outdir/fault-status.failed.json"
  log "  [실패] Chaos Mesh fault가 제한 시간 안에 주입 완료 상태가 되지 않음"
  return 1
}

inject_scenario() {
  local scen="$1" dir="$SCENARIO_DIR/$scen" outdir="$RESULT_DIR/$scen"
  mkdir -p "$outdir"
  log "[주입] $scen"
  if [ -f "$dir/prepare.sh" ]; then
    SCENARIO_STATE_DIR="$outdir/state" bash "$dir/prepare.sh" \
      2>&1 | tee "$outdir/prepare.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || return 1
  fi
  if [ -f "$dir/inject.sh" ]; then
    SCENARIO_STATE_DIR="$outdir/state" bash "$dir/inject.sh" 2>&1 | tee "$outdir/inject.log"
    return "${PIPESTATUS[0]}"
  elif [ -f "$dir/fault.yaml" ]; then
    : > "$outdir/inject.log"
    kubectl delete -f "$dir/fault.yaml" --ignore-not-found --wait=true \
      --timeout=60s 2>&1 | tee -a "$outdir/inject.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || return 1
    kubectl apply -f "$dir/fault.yaml" 2>&1 | tee -a "$outdir/inject.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || return 1
    verify_chaos_injected "$dir/fault.yaml" "$outdir"
    return $?
  else
    log "  [실패] inject.sh / fault.yaml 없음"
    return 1
  fi
}

recover_scenario() {
  local scen="$1"
  local dir="$SCENARIO_DIR/$scen" outdir="$RESULT_DIR/$scen"
  local recover_log="$outdir/recover.log"
  local previous_attempt="${RECOVERY_ATTEMPTS[$scen]:-0}"
  local attempt=$((previous_attempt + 1))
  local -a pipeline_status=()
  mkdir -p "$outdir"
  RECOVERY_ATTEMPTS["$scen"]="$attempt"
  if [ "$attempt" -eq 1 ]; then
    : > "$recover_log" || return 1
  fi
  if ! printf '\n=== recover attempt=%s scenario=%s ===\n' "$attempt" "$scen" \
    | tee -a "$recover_log"; then
    return 1
  fi
  log "[복구] $scen"
  if [ -f "$dir/recover.sh" ]; then
    SCENARIO_STATE_DIR="$outdir/state" bash "$dir/recover.sh" 2>&1 \
      | tee -a "$recover_log"
    pipeline_status=("${PIPESTATUS[@]}")
    [ "${pipeline_status[0]}" -eq 0 ] \
      && [ "${pipeline_status[1]}" -eq 0 ]
    return $?
  elif [ -f "$dir/fault.yaml" ]; then
    kubectl delete -f "$dir/fault.yaml" --ignore-not-found --wait=true \
      --timeout=60s 2>&1 | tee -a "$recover_log"
    pipeline_status=("${PIPESTATUS[@]}")
    [ "${pipeline_status[0]}" -eq 0 ] \
      && [ "${pipeline_status[1]}" -eq 0 ]
    return $?
  else
    return 0
  fi
}

wait_for_detection() {
  local tries=15 t err successful_polls=0
  for t in $(seq 1 "$tries"); do
    if ! invalidate_cache; then
      log "  [$t/$tries] 캐시 무효화 API 실패"
      sleep 2
      continue
    fi
    sleep 1
    if ! err="$(get_error_count)"; then
      log "  [$t/$tries] 평가 API/JSON 실패"
      sleep 2
      continue
    fi
    successful_polls=$((successful_polls + 1))
    [ "$err" -gt 0 ] && log "  [감지] error_nodes=$err (시도 $t)" && return 0
    log "  [$t/$tries] 탐지 대기 (error=$err)"; sleep 2
  done
  if [ "$successful_polls" -eq 0 ]; then
    log "  [실패] 유효한 탐지 API 응답이 한 번도 없음"
    return 2
  fi
  log "  [미탐지] 유효한 응답은 있었지만 error_nodes=0"
  return 1
}

capture_observation_evidence() {
  local scen="$1" detectable="$2" detected="$3" detection_api_ok="$4"
  local outdir="$RESULT_DIR/$scen"
  local evidence="$outdir/observation-evidence.txt"
  local settle_seconds="${SCENARIO_SETTLE_SECONDS[$scen]}"
  local recovery_settle_seconds="${SCENARIO_RECOVERY_SETTLE_SECONDS[$scen]}"
  local resource

  {
    printf 'captured_at_utc=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    printf 'scenario=%s\ndetectable=%s\ndetected=%s\ndetection_api_ok=%s\n' \
      "$scen" "$detectable" "$detected" "$detection_api_ok"
    printf 'observation_settle_seconds=%s\n' "$settle_seconds"
    printf 'recovery_settle_seconds=%s\n' "$recovery_settle_seconds"
    printf '\n[NODES]\n'
    kubectl get nodes -o wide
    printf '\n[PODS]\n'
    kubectl get pods -n "$NS" -o wide
    printf '\n[DEPLOYMENTS]\n'
    kubectl get deployments -n "$NS" -o wide
    printf '\n[REPLICASETS]\n'
    kubectl get replicasets -n "$NS" -o wide
    printf '\n[SERVICES]\n'
    kubectl get services -n "$NS" -o wide
    printf '\n[ENDPOINTSLICES]\n'
    kubectl get endpointslices.discovery.k8s.io -n "$NS" -o wide
    printf '\n[CHAOS]\n'
    for resource in podchaos networkchaos dnschaos iochaos stresschaos podiochaos; do
      kubectl get "$resource" -n "$NS" -o wide 2>&1 || true
    done
    printf '\n[RECENT_EVENTS]\n'
    kubectl get events -n "$NS" --sort-by=.metadata.creationTimestamp 2>&1 | tail -n 120
    printf '\n[BACKEND_HEALTH]\n'
    curl -sS --connect-timeout 5 --max-time 30 "$API_BASE/api/health" || true
    if [ -n "$PROMETHEUS_URL" ]; then
      printf '\n[PROMETHEUS_UP]\n'
      curl -sS --connect-timeout 5 --max-time 30 \
        --get --data-urlencode 'query=up' "$PROMETHEUS_URL/api/v1/query" || true
    else
      printf '\n[PROMETHEUS_UP]\nPROMETHEUS_URL not configured for runner\n'
    fi
    printf '\n'
  } > "$evidence" 2>&1
}

diagnose_observation_failure() {
  local scen="$1" detectable="$2" detected="$3"
  local detection_api_ok="$4" evaluation_ok="$5"
  local outdir="$RESULT_DIR/$scen"

  python3 - "$outdir" "$scen" "$detectable" "$detected" \
    "$detection_api_ok" "$evaluation_ok" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

outdir, scenario, detectable, detected, api_ok, evaluation_ok = sys.argv[1:]

def load_json(name):
    path = os.path.join(outdir, name)
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except Exception:
        return None

snapshot = load_json("_snapshot.json")
failed_fault_status = load_json("fault-status.failed.json")
fault_status = failed_fault_status or load_json("fault-status.json")
mode_errors = {}
for mode in ("hybrid", "dfs_only", "llm_only"):
    payload = load_json(f"{mode}.json")
    mode_errors[mode] = None if payload is None else payload.get("error_node_count")

snapshot_errors = None if snapshot is None else snapshot.get("error_node_count")
fault_conditions = {}
fault_errors = []
if isinstance(fault_status, dict):
    status = fault_status.get("status", {})
    fault_conditions = {
        item.get("type"): item.get("status")
        for item in status.get("conditions", [])
        if isinstance(item, dict)
    }
    for record in status.get("experiment", {}).get("containerRecords", []):
        for event in record.get("events", []):
            message = event.get("message")
            if event.get("type") == "Failed" and isinstance(message, str):
                message = message.strip()
                if message and message not in fault_errors:
                    fault_errors.append(message)

if failed_fault_status is not None or detected == "not_run":
    classification = "fault_injection_failure"
    explanation = "Chaos fault가 주입 완료 상태에 도달하지 못해 평가 전 안전 중단했습니다."
elif detectable != "true":
    classification = "ground_truth_undetectable"
    explanation = "Ground Truth상 Kubernetes 상태 기반 엔진이 감지하지 못하는 장애입니다."
elif api_ok != "true":
    classification = "detection_api_failure"
    explanation = "감지 polling 중 유효한 API 응답을 확보하지 못했습니다."
elif detected == "false" and isinstance(snapshot_errors, int) and snapshot_errors > 0:
    classification = "detection_window_miss"
    explanation = "polling 구간에서는 0이었지만 평가 snapshot에는 오류 노드가 존재했습니다."
elif detected == "false":
    classification = "engine_observation_gap"
    explanation = "장애 주입 증거는 저장됐지만 엔진 snapshot의 error_node_count가 0입니다."
elif evaluation_ok != "true":
    classification = "mode_evaluation_failure"
    explanation = "장애는 감지됐지만 하나 이상의 모드 평가가 실패했습니다."
else:
    classification = "detected"
    explanation = "장애 감지와 모드 평가가 정상 완료됐습니다."

result = {
    "schema_version": 1,
    "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "scenario": scenario,
    "detectable": detectable == "true",
    "detected": detected,
    "detection_api_ok": api_ok == "true",
    "evaluation_ok": evaluation_ok == "true",
    "classification": classification,
    "explanation": explanation,
    "snapshot_error_node_count": snapshot_errors,
    "mode_error_node_count": mode_errors,
    "fault_conditions": fault_conditions,
    "fault_errors": fault_errors[:5],
    "evidence_file": "observation-evidence.txt",
}
with open(os.path.join(outdir, "observation-diagnosis.json"), "w", encoding="utf-8") as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
PY
}

planned_mode_seconds() {
  case "$1" in
    hybrid|llm_only) printf '%s\n' "$EVAL_TIMEOUT_SECONDS" ;;
    dfs_only) printf '%s\n' "$DFS_MODE_PLANNED_SECONDS" ;;
    *) return 1 ;;
  esac
}

planned_all_modes_seconds() {
  local snapshot_error_count="$1"
  local mode duration total=0 llm_mode_count=0
  if ! [[ "$snapshot_error_count" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  for mode in "${MODES[@]}"; do
    duration="$(planned_mode_seconds "$mode")" || return 1
    total=$((total + duration))
    if [ "$snapshot_error_count" -gt 0 ]; then
      case "$mode" in
        hybrid|llm_only) llm_mode_count=$((llm_mode_count + 1)) ;;
      esac
    fi
  done
  total=$((total + llm_mode_count * LLM_MODE_INTERVAL_SECONDS))
  printf '%s\n' "$total"
}

guard_snapshot_deadline() {
  local required_seconds="$1" context="$2"
  local remaining_seconds

  if [ -z "${ACTIVE_SNAPSHOT_ID:-}" ] || [ "$ACTIVE_SNAPSHOT_DEADLINE" -le 0 ]; then
    log "    [실패] $context: 활성 snapshot deadline이 없음"
    return 1
  fi
  remaining_seconds=$((ACTIVE_SNAPSHOT_DEADLINE - SECONDS))
  if [ "$remaining_seconds" -lt $((required_seconds + SNAPSHOT_ACQUIRE_MARGIN_SECONDS)) ]; then
    log "    [실패] $context: snapshot 잔여 ${remaining_seconds}초, 필요 $((required_seconds + SNAPSHOT_ACQUIRE_MARGIN_SECONDS))초"
    return 1
  fi
  return 0
}

throttle_llm_mode() {
  local mode="$1" snapshot_error_count="$2"
  local now elapsed wait_seconds planned_seconds
  case "$mode" in
    hybrid|llm_only) ;;
    *) return 0 ;;
  esac
  if ! [[ "$snapshot_error_count" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [ "$snapshot_error_count" -eq 0 ]; then
    log "    [$mode] error_nodes=0, provider 미호출 계약으로 throttle 생략"
    return 0
  fi
  [ "$LLM_MODE_INTERVAL_SECONDS" -eq 0 ] && return 0

  now="$(date +%s)"
  if [ "$LAST_LLM_MODE_EPOCH" -gt 0 ]; then
    elapsed=$((now - LAST_LLM_MODE_EPOCH))
    if [ "$elapsed" -lt "$LLM_MODE_INTERVAL_SECONDS" ]; then
      wait_seconds=$((LLM_MODE_INTERVAL_SECONDS - elapsed))
      log "    [$mode] 무료 API 토큰 한도 대기 ${wait_seconds}초"
      planned_seconds="$(planned_mode_seconds "$mode")" || return 1
      guard_snapshot_deadline $((wait_seconds + planned_seconds)) \
        "$mode throttle 대기" || return 1
      sleep "$wait_seconds"
    fi
  fi
  LAST_LLM_MODE_EPOCH="$(date +%s)"
}

call_all_modes() {
  local scen="$1"
  local outdir="$RESULT_DIR/$scen"
  local snapshot_tmp="$outdir/_snapshot.json.tmp"
  local snapshot_id snapshot_error_count snapshot_captured_at snapshot_ttl
  local snapshot_request_started
  local mode response_tmp validation chains ai_len error_count planned_seconds
  local http_code curl_rc
  local failed=0

  mkdir -p "$outdir"
  log "  [동일 snapshot 평가: ${MODES[*]}]"

  if [ -n "${ACTIVE_SNAPSHOT_ID:-}" ] && ! release_active_snapshot; then
    log "    [실패] 이전 snapshot을 해제하지 못해 새 snapshot을 예약하지 않음: $ACTIVE_SNAPSHOT_ID"
    return 1
  fi
  if ! snapshot_id="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" \
    || ! [[ "$snapshot_id" =~ ^[0-9a-f]{32}$ ]]; then
    log "    [실패] client snapshot ID 생성 실패"
    return 1
  fi
  ACTIVE_SNAPSHOT_ID="$snapshot_id"
  ACTIVE_SNAPSHOT_DEADLINE=0

  snapshot_request_started=$SECONDS
  http_code="$(curl -sS --connect-timeout 10 --max-time 120 \
    -X POST "$SNAPSHOT_API?snapshot_id=$snapshot_id" \
    -o "$snapshot_tmp" -w '%{http_code}')"
  curl_rc=$?
  printf 'curl_rc=%s\nhttp_code=%s\n' "$curl_rc" "$http_code" \
    > "$outdir/_snapshot.http.txt"
  if [ "$curl_rc" -ne 0 ] || ! [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    log "    [실패] 평가 snapshot 생성 API 오류"
    [ -f "$snapshot_tmp" ] && mv -f "$snapshot_tmp" "$outdir/_snapshot.failed.json"
    release_active_snapshot || log "    [경고] 실패한 snapshot 예약 해제 재시도 필요: $snapshot_id"
    return 1
  fi

  if ! validation="$(python3 -c 'import json,sys
d=json.load(sys.stdin)
expected_snapshot=sys.argv[1]
snapshot_id=d.get("snapshot_id", "")
if d.get("status") != "success" or snapshot_id != expected_snapshot:
    raise ValueError("invalid snapshot response")
errors=d.get("error_node_count")
captured=d.get("captured_at")
ttl=d.get("expires_in_sec")
if not isinstance(errors, int) or isinstance(errors, bool): raise TypeError("invalid error count")
if not isinstance(captured, str) or not captured: raise TypeError("invalid captured_at")
if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0: raise TypeError("invalid expires_in_sec")
print(f"{errors}|{captured}|{ttl}")' "$snapshot_id" < "$snapshot_tmp")"; then
    log "    [실패] 평가 snapshot 응답 검증 실패"
    mv -f "$snapshot_tmp" "$outdir/_snapshot.failed.json"
    release_active_snapshot || log "    [경고] 실패한 snapshot 예약 해제 재시도 필요: $snapshot_id"
    return 1
  fi
  IFS='|' read -r snapshot_error_count snapshot_captured_at snapshot_ttl <<< "$validation"
  ACTIVE_SNAPSHOT_DEADLINE=$((snapshot_request_started + snapshot_ttl))
  if ! mv -f "$snapshot_tmp" "$outdir/_snapshot.json"; then
    log "    [실패] snapshot 산출물 저장 실패"
    release_active_snapshot || true
    return 1
  fi
  log "    [snapshot] $snapshot_id (TTL ${snapshot_ttl}초)"

  planned_seconds="$(planned_all_modes_seconds "$snapshot_error_count")" || {
    release_active_snapshot || true
    return 1
  }
  if ! guard_snapshot_deadline "$planned_seconds" "전체 mode 실행 예산"; then
    release_active_snapshot || true
    return 1
  fi

  for mode in "${MODES[@]}"; do
    if ! throttle_llm_mode "$mode" "$snapshot_error_count"; then
      failed=1
      break
    fi
    planned_seconds="$(planned_mode_seconds "$mode")" || {
      failed=1
      break
    }
    if ! guard_snapshot_deadline "$planned_seconds" "$mode 호출"; then
      failed=1
      break
    fi
    response_tmp="$outdir/${mode}.json.tmp"
    http_code="$(curl -sS --connect-timeout 10 --max-time "$planned_seconds" \
      --get \
      --data-urlencode "mode=$mode" \
      --data-urlencode "snapshot_id=$snapshot_id" \
      "$EVAL_API" -o "$response_tmp" -w '%{http_code}')"
    curl_rc=$?
    printf 'curl_rc=%s\nhttp_code=%s\n' "$curl_rc" "$http_code" \
      > "$outdir/${mode}.http.txt"
    if [ "$curl_rc" -ne 0 ] || ! [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
      log "    [$mode] 실패: HTTP/전송 오류"
      [ -f "$response_tmp" ] && mv -f "$response_tmp" "$outdir/${mode}.failed.json"
      failed=1
      continue
    fi

    if ! validation="$(python3 -c 'import json,sys
import math
expected_mode, expected_snapshot, expected_errors, expected_captured = sys.argv[1:5]
d=json.load(sys.stdin)
if d.get("status") != "success": raise ValueError("status is not success")
if d.get("mode") != expected_mode: raise ValueError("mode mismatch")
if d.get("snapshot_id") != expected_snapshot: raise ValueError("snapshot mismatch")
if d.get("snapshot_captured_at") != expected_captured: raise ValueError("captured_at mismatch")
timing=d.get("timing_sec")
if not isinstance(timing, (int, float)) or isinstance(timing, bool):
    raise TypeError("invalid timing")
if not math.isfinite(timing) or timing < 0: raise ValueError("invalid timing value")
data=d.get("data")
if not isinstance(data, dict): raise TypeError("data is not an object")
chains=data.get("total_chains", 0)
chain_items=data.get("chains")
ai_text=data.get("ai_analysis", "")
errors=d.get("error_node_count", 0)
if not isinstance(chains, int) or isinstance(chains, bool): raise TypeError("invalid chain count")
if not isinstance(chain_items, list) or chains != len(chain_items): raise ValueError("chains mismatch")
if not isinstance(ai_text, str): raise TypeError("invalid ai_analysis")
if not isinstance(errors, int) or isinstance(errors, bool): raise TypeError("invalid error count")
if errors != int(expected_errors): raise ValueError("error count mismatch")
if errors == 0 and (chains != 0 or chain_items): raise ValueError("zero-error response contains chains")
if expected_mode in {"hybrid", "llm_only"}:
    if errors > 0:
        if data.get("ai_status") != "success": raise ValueError("LLM analysis failed")
        if not ai_text.strip(): raise ValueError("empty LLM analysis")
    else:
        if data.get("ai_status") != "not_applicable": raise ValueError("zero-error LLM status mismatch")
        if ai_text: raise ValueError("zero-error response contains LLM analysis")
print(f"{chains}|{len(ai_text)}|{errors}")' \
      "$mode" "$snapshot_id" "$snapshot_error_count" "$snapshot_captured_at" \
      < "$response_tmp")"; then
      log "    [$mode] 실패: JSON/status/snapshot 검증 오류"
      mv -f "$response_tmp" "$outdir/${mode}.failed.json"
      failed=1
      continue
    fi

    if ! mv -f "$response_tmp" "$outdir/${mode}.json"; then
      log "    [$mode] 실패: 산출물 저장 오류"
      failed=1
      continue
    fi
    IFS='|' read -r chains ai_len error_count <<< "$validation"
    if [ "$mode" = "llm_only" ]; then
      log "    [$mode] ai_analysis 길이=$ai_len, error_nodes=$error_count"
    else
      log "    [$mode] total_chains=$chains, error_nodes=$error_count"
    fi
  done

  if ! release_active_snapshot; then
    log "    [경고] snapshot release 실패 (TTL로 자동 정리됨): $snapshot_id"
    failed=1
  fi

  return "$failed"
}

write_status() {
  local scen="$1" status="$2" detectable="$3" detected="$4" reason="$5"
  local outdir="$RESULT_DIR/$scen"
  local status_tmp="$outdir/status.txt.tmp"
  mkdir -p "$outdir" || return 1
  if ! printf 'scenario=%s\nstatus=%s\ndetectable=%s\ndetected=%s\nreason=%s\n' \
    "$scen" "$status" "$detectable" "$detected" "$reason" > "$status_tmp"; then
    return 1
  fi
  mv -f "$status_tmp" "$outdir/status.txt" || return 1
  if [ ! -f "$RESULT_DIR/_summary.tsv" ]; then
    printf 'scenario\tstatus\tdetectable\tdetected\treason\n' > "$RESULT_DIR/_summary.tsv" || return 1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$scen" "$status" "$detectable" "$detected" "$reason" >> "$RESULT_DIR/_summary.tsv" || return 1
  if [ "$status" = "invalid" ]; then
    RUN_FAILURES=$((RUN_FAILURES + 1))
  fi
  return 0
}

record_status() {
  if ! write_status "$@"; then
    log "[실패] 상태 산출물을 저장할 수 없음: $1"
    exit 1
  fi
}

# ── 메인 루프 ──────────────────────────────────────────
for idx in "${!SCENARIOS[@]}"; do
  scen="${SCENARIOS[$idx]}"
  num=$((idx+1))
  settle_seconds="${SCENARIO_SETTLE_SECONDS[$scen]}"
  mkdir -p "$RESULT_DIR/$scen" || exit 1
  detectable="false"
  is_gt_detectable "$scen" && detectable="true"

  echo; echo "=========================================="; echo " [$num/${#SCENARIOS[@]}] — $scen"; echo "=========================================="

  if is_in_list "$scen" "${EXCLUDED_SCENARIOS[@]}"; then
    record_status "$scen" "excluded" "$detectable" "not_applicable" "excluded by EXCLUDED_SCENARIOS_CSV"
    continue
  fi

  if ! wait_cluster_stable; then
    record_status "$scen" "invalid" "$detectable" "not_run" "cluster not stable before injection"
    continue
  fi
  delete_zero_rs

  if ! wait_baseline_zero; then
    record_status "$scen" "invalid" "$detectable" "not_run" "baseline not zero before injection"
    continue
  fi

  ACTIVE_SCENARIO="$scen"
  if ! inject_scenario "$scen"; then
    injection_recovery_ok="true"
    recover_scenario "$scen" || injection_recovery_ok="false"
    wait_recovery_settle "$scen" || injection_recovery_ok="false"
    wait_cluster_stable || injection_recovery_ok="false"
    wait_baseline_zero || injection_recovery_ok="false"
    [ "$injection_recovery_ok" = "true" ] && ACTIVE_SCENARIO=""
    capture_observation_evidence "$scen" "$detectable" "not_run" "true" || true
    diagnose_observation_failure "$scen" "$detectable" "not_run" "true" "false" || true
    record_status "$scen" "invalid" "$detectable" "not_run" "fault injection failed"
    exit 1
  fi

  log "  [대기] observation settle ${settle_seconds}초..."
  [ "$settle_seconds" -eq 0 ] || sleep "$settle_seconds"

  detected="not_applicable"
  detection_api_ok="true"
  if [ "$detectable" = "false" ]; then
    log "  [스킵] Ground Truth detectable=false"
  else
    if wait_for_detection; then
      detected="true"
    else
      detection_rc=$?
      if [ "$detection_rc" -eq 1 ]; then
        detected="false"
      else
        detected="unknown"
        detection_api_ok="false"
      fi
    fi
  fi

  capture_observation_evidence \
    "$scen" "$detectable" "$detected" "$detection_api_ok" || true
  evaluation_ok="true"
  call_all_modes "$scen" || evaluation_ok="false"
  diagnose_observation_failure \
    "$scen" "$detectable" "$detected" "$detection_api_ok" "$evaluation_ok" || true
  recovery_ok="true"
  recover_scenario "$scen" || recovery_ok="false"
  wait_recovery_settle "$scen" || recovery_ok="false"
  wait_cluster_stable || recovery_ok="false"

  if ! wait_baseline_zero; then
    recovery_ok="false"
  fi

  if [ "$recovery_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "recovery incomplete or dirty baseline after scenario"
    exit 1
  fi
  ACTIVE_SCENARIO=""

  if [ "$detection_api_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "detection API produced no valid response"
    exit 1
  fi

  if [ "$evaluation_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "snapshot creation or mode evaluation failed"
    exit 1
  fi

  if [ "$detected" = "false" ]; then
    record_status "$scen" "valid" "$detectable" "$detected" "completed; valid false negative"
  else
    record_status "$scen" "valid" "$detectable" "$detected" "completed"
  fi
done

echo; echo "=========================================="; echo " 완료: $RESULT_DIR"; echo " invalid scenarios: $RUN_FAILURES"; echo "=========================================="
if [ "$RUN_FAILURES" -gt 0 ]; then
  exit 1
fi

if ! python3 - "$RESULT_DIR/_summary.tsv" "${SCENARIOS[@]}" <<'PY'
import csv
import sys

path, *expected = sys.argv[1:]
with open(path, newline="", encoding="utf-8") as stream:
    reader = csv.DictReader(stream, delimiter="\t")
    rows = list(reader)
required = ["scenario", "status", "detectable", "detected", "reason"]
if reader.fieldnames != required:
    raise SystemExit("invalid summary schema")
names = [row["scenario"] for row in rows]
if len(rows) != len(expected) or len(set(names)) != len(names) or set(names) != set(expected):
    raise SystemExit("incomplete or duplicate summary")
PY
then
  log "[실패] 최종 summary 무결성 검증 실패"
  exit 1
fi

pointer_tmp="$LATEST_POINTER.tmp.$$"
if ! printf '%s\n' "$RESULT_DIR" > "$pointer_tmp" || ! mv -f "$pointer_tmp" "$LATEST_POINTER"; then
  rm -f "$pointer_tmp"
  log "[실패] latest 결과 포인터 저장 실패"
  exit 1
fi
exit 0
