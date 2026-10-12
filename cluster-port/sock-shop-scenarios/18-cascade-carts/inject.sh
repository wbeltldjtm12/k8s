#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl
log '18-cascade-carts: carts-db Pod 제거 뒤 carts Pod를 순차 제거'
delete_pods_by_label carts-db
sleep 2
delete_pods_by_label carts
