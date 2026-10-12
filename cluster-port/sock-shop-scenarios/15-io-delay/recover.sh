#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment carts-db

rc=0
if ! kubectl delete -f "$SCRIPT_DIR/fault.yaml" --ignore-not-found --wait=true \
  --timeout=60s; then
  # Chaos Mesh can retain its records finalizer when toda never started.  Only
  # force-remove that finalizer when the CR itself proves that nothing was
  # injected; an injected fault must never be force-cleaned this way.
  if kubectl get -f "$SCRIPT_DIR/fault.yaml" -o json 2>/dev/null \
    | python3 -c 'import json,sys
d=json.load(sys.stdin)
status=d.get("status", {})
conditions={x.get("type"): x.get("status") for x in status.get("conditions", []) if isinstance(x, dict)}
records=status.get("experiment", {}).get("containerRecords", [])
safe=(
    bool(d.get("metadata", {}).get("deletionTimestamp"))
    and bool(records)
    and conditions.get("AllInjected") != "True"
    and all(r.get("injectedCount", 0) == 0 for r in records)
)
raise SystemExit(0 if safe else 1)'; then
    log '15-io-delay: injectedCount=0을 확인해 멈춘 IOChaos records finalizer를 제거'
    kubectl patch iochaos kubein-15-io-delay -n "$NS" --type=merge \
      -p '{"metadata":{"finalizers":[]}}' || rc=1
    kubectl wait --for=delete iochaos/kubein-15-io-delay -n "$NS" \
      --timeout=30s || rc=1
  else
    log '15-io-delay: 주입 이력이 없다는 안전 조건을 확인하지 못해 finalizer 강제 제거를 거부'
    rc=1
  fi
fi

mapfile -t stale_podiochaos < <(
  kubectl get podiochaos -n "$NS" -o json 2>/dev/null \
    | python3 -c 'import json,sys
data=json.load(sys.stdin)
source="sock-shop/kubein-15-io-delay"
for item in data.get("items", []):
    actions=item.get("spec", {}).get("actions", [])
    if any(action.get("source") == source for action in actions):
        print(item["metadata"]["name"])' 2>/dev/null
)
for name in "${stale_podiochaos[@]}"; do
  log "15-io-delay: 잔류 내부 PodIOChaos 삭제: $name"
  kubectl delete podiochaos "$name" -n "$NS" --wait=true \
    --timeout=30s || rc=1
done

restore_deployment_template carts-db || rc=1
wait_rollout carts-db || rc=1
if [ -n "$(
  kubectl get pods -n "$NS" -l kubein-iochaos-target=true \
    -o name 2>/dev/null
)" ]; then
  kubectl wait --for=delete pod -n "$NS" \
    -l kubein-iochaos-target=true --timeout=120s || rc=1
fi
exit "$rc"
