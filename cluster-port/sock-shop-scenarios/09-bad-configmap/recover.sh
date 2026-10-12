#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl
rc=0
restore_deployment_template front-end || rc=1
kubectl delete configmap kubein-front-end-bad-config -n "$NS" --ignore-not-found || rc=1
wait_rollout front-end || rc=1
exit "$rc"
