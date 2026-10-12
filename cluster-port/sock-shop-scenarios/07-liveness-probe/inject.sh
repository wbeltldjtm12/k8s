#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment catalogue
log '07-liveness-probe: catalogue livenessProbe를 실패 경로로 변경'
set_failing_probe catalogue liveness
