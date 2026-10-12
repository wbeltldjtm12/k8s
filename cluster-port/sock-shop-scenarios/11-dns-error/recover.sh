#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl
rc=0
kubectl delete -f "$SCRIPT_DIR/fault.yaml" --ignore-not-found --wait=true \
  --timeout=60s || rc=1
restore_deployment_template shipping || rc=1
wait_rollout shipping || rc=1
if [ -n "$(
  kubectl get pods -n "$NS" -l kubein-dnschaos-target=true \
    -o name 2>/dev/null
)" ]; then
  kubectl wait --for=delete pod -n "$NS" \
    -l kubein-dnschaos-target=true --timeout=120s || rc=1
fi
exit "$rc"
