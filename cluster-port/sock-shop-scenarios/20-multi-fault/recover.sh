#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl
rc=0
restore_service_selector user || rc=1
restore_deployment_template payment || rc=1
wait_rollout payment || rc=1
exit "$rc"
