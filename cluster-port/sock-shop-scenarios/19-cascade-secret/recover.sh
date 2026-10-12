#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl
rc=0
restore_deployment_template catalogue-db || rc=1
kubectl delete secret kubein-catalogue-db-cascade-secret -n "$NS" --ignore-not-found || rc=1
wait_rollout catalogue-db || rc=1
exit "$rc"
