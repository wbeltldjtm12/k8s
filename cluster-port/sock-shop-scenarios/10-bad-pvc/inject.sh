#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment orders-db
log '10-bad-pvc: orders-db에 존재하지 않는 PVC를 mount하도록 변경'
add_missing_pvc_mount orders-db kubein-orders-db-missing-pvc
