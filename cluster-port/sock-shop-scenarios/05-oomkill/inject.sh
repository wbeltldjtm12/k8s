#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment orders
log '05-oomkill: orders memory limit을 32Mi로 낮춰 OOMKilled 유도'
set_memory_limit orders 32Mi
