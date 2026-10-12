#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment catalogue-db
log '08-secret-delete: catalogue-db가 참조하는 실험용 Secret 삭제'
add_then_remove_secret_env catalogue-db kubein-catalogue-db-secret
