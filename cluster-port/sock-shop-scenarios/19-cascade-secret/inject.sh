#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment catalogue-db
log '19-cascade-secret: catalogue-db의 공유 Secret 참조를 만든 뒤 삭제'
add_then_remove_secret_env catalogue-db kubein-catalogue-db-cascade-secret
