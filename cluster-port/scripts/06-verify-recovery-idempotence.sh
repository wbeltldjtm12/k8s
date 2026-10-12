#!/usr/bin/env bash
# Verify that a manual scenario can be recovered repeatedly without drifting
# from the exact Deployment Pod template captured before injection.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCENARIO_DIR="${SCENARIO_DIR:-$REPO_ROOT/cluster-port/sock-shop-scenarios}"
SCENARIO="${1:-09-bad-configmap}"
DEPLOYMENT="${2:-front-end}"
NS="${KUBEIN_SCENARIO_NAMESPACE:-sock-shop}"
API_BASE="${API_BASE:-http://localhost:8000}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-$SCENARIO_DIR/results/recovery-verification}"
RUN_LOCK_FILE="${RUN_LOCK_FILE:-/tmp/kubein-ablation.lock}"

command -v flock >/dev/null 2>&1
exec 9>"$RUN_LOCK_FILE"
flock -n 9 || {
  echo "[오류] 다른 KUBEIN ablation 실행이 진행 중입니다." >&2
  exit 75
}

INJECT_SCRIPT="$SCENARIO_DIR/$SCENARIO/inject.sh"
RECOVER_SCRIPT="$SCENARIO_DIR/$SCENARIO/recover.sh"
[ -f "$INJECT_SCRIPT" ] && [ -f "$RECOVER_SCRIPT" ] || {
  echo "[오류] inject/recover script가 없습니다: $SCENARIO" >&2
  exit 2
}

mkdir -p "$EVIDENCE_ROOT"
EVIDENCE_DIR="$(mktemp -d "$EVIDENCE_ROOT/${SCENARIO}_$(date -u +%Y%m%dT%H%M%SZ)_XXXXXX")"
STATE_DIR="$EVIDENCE_DIR/state"
mkdir -p "$STATE_DIR"
ACTIVE=0

hash_template() {
  kubectl get deployment "$DEPLOYMENT" -n "$NS" -o json \
    | python3 -c 'import hashlib,json,sys
template=json.load(sys.stdin)["spec"]["template"]
canonical=json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(canonical).hexdigest())'
}

cleanup_on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$ACTIVE" -eq 1 ]; then
    SCENARIO_STATE_DIR="$STATE_DIR" bash "$RECOVER_SCRIPT" \
      >> "$EVIDENCE_DIR/emergency-recover.log" 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup_on_exit EXIT

hash_template > "$EVIDENCE_DIR/before.sha256"
ACTIVE=1
SCENARIO_STATE_DIR="$STATE_DIR" bash "$INJECT_SCRIPT" \
  > "$EVIDENCE_DIR/inject.log" 2>&1
hash_template > "$EVIDENCE_DIR/injected.sha256"

SCENARIO_STATE_DIR="$STATE_DIR" bash "$RECOVER_SCRIPT" \
  > "$EVIDENCE_DIR/recover-1.log" 2>&1
hash_template > "$EVIDENCE_DIR/after-1.sha256"

SCENARIO_STATE_DIR="$STATE_DIR" bash "$RECOVER_SCRIPT" \
  > "$EVIDENCE_DIR/recover-2.log" 2>&1
hash_template > "$EVIDENCE_DIR/after-2.sha256"
ACTIVE=0

before="$(<"$EVIDENCE_DIR/before.sha256")"
injected="$(<"$EVIDENCE_DIR/injected.sha256")"
after_1="$(<"$EVIDENCE_DIR/after-1.sha256")"
after_2="$(<"$EVIDENCE_DIR/after-2.sha256")"

[ "$before" != "$injected" ] || {
  echo "[오류] 주입 후 Deployment template이 변하지 않았습니다." >&2
  exit 1
}
[ "$before" = "$after_1" ] && [ "$before" = "$after_2" ] || {
  echo "[오류] 반복 복구 후 원본 Deployment template과 일치하지 않습니다." >&2
  exit 1
}

baseline=-1
for attempt in $(seq 1 12); do
  curl -fsS --connect-timeout 5 --max-time 30 \
    -X POST "$API_BASE/api/cache/invalidate" >/dev/null
  sleep 2
  baseline="$(
    curl -fsS --connect-timeout 5 --max-time 60 \
      --get --data-urlencode mode=dfs_only "$API_BASE/api/evaluate" \
      | python3 -c 'import json,sys
data=json.load(sys.stdin)
value=data.get("error_node_count")
if not isinstance(value, int) or isinstance(value, bool):
    raise SystemExit("invalid error_node_count")
print(value)'
  )"
  [ "$baseline" -eq 0 ] && break
  sleep 3
done
[ "$baseline" -eq 0 ] || {
  echo "[오류] 반복 복구 후 baseline error_node_count=$baseline" >&2
  exit 1
}

printf 'scenario=%s\ndeployment=%s\nbefore=%s\ninjected=%s\nafter_1=%s\nafter_2=%s\nbaseline_error_node_count=%s\n' \
  "$SCENARIO" "$DEPLOYMENT" "$before" "$injected" "$after_1" "$after_2" "$baseline" \
  > "$EVIDENCE_DIR/result.txt"

echo "[OK] recovery idempotence verified: $SCENARIO/$DEPLOYMENT"
echo "[OK] evidence: $EVIDENCE_DIR"
