#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/scenario-lib.sh"
init_state "$SCRIPT_DIR"
need_kubectl
need_deployment catalogue

log '02-pod-failure: PodChaos 복구 후 restart residue가 없는 catalogue Pod로 교체'
kubectl delete -f "$SCRIPT_DIR/fault.yaml" \
  --ignore-not-found --wait=true --timeout=60s
kubectl delete pod -n "$NS" -l name=catalogue \
  --ignore-not-found --wait=true --timeout=60s
wait_rollout catalogue
