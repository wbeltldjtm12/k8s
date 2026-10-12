#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment carts-db
log '15-io-delay 준비: IOChaos toda가 시작되도록 carts-db root filesystem을 일시적으로 writable로 변경'
set_root_filesystem_writable_for_target carts-db kubein-iochaos-target
wait_rollout carts-db
