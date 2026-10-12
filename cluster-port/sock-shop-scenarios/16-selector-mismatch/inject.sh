#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_service user
save_service_selector user
log '16-selector-mismatch: user Service selector를 존재하지 않는 label로 변경'
set_selector_mismatch user
