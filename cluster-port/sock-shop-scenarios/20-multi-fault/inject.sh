#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment payment; need_service user
save_service_selector user
log '20-multi-fault: payment readiness failure와 user selector mismatch 동시 주입'
set_failing_probe payment readiness
set_selector_mismatch user
