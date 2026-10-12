#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"; need_kubectl; need_deployment front-end
log '09-bad-configmap: 잘못된 NODE_OPTIONS ConfigMap을 front-end에 주입'
add_bad_configmap_env front-end kubein-front-end-bad-config
