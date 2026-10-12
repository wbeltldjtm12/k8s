#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment shipping
log '11-dns-error 준비: DNSChaos가 resolv.conf backup을 만들 수 있도록 shipping root filesystem을 일시적으로 writable로 변경'
set_root_filesystem_writable shipping
wait_rollout shipping
