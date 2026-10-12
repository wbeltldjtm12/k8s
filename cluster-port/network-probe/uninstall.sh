#!/usr/bin/env bash
# Remove only the lab sidecar and its PodMonitor; application containers stay intact.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="${KUBEIN_SCENARIO_NAMESPACE:-sock-shop}"
PROBE_IMAGE_PULL_SECRET="${PROBE_IMAGE_PULL_SECRET:-}"
ROLLOUT_TIMEOUT_SECONDS="${PROBE_ROLLOUT_TIMEOUT_SECONDS:-360}"

if [[ ! "$ROLLOUT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] PROBE_ROLLOUT_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

for workload in shipping payment front-end; do
  kubectl get deployment "$workload" -n "$NS" >/dev/null 2>&1 || continue
  cat <<'EOF' | kubectl patch deployment "$workload" -n "$NS" --type=strategic --patch-file=/dev/stdin
spec:
  template:
    spec:
      containers:
        - name: kubein-netprobe
          $patch: delete
EOF
  kubectl patch deployment "$workload" -n "$NS" --type=merge \
    -p '{"spec":{"template":{"metadata":{"labels":{"kubein.io/network-probe":null,"kubein.io/network-probe-type":null,"kubein.io/network-probe-target":null}}}}}'
  if [ -n "$PROBE_IMAGE_PULL_SECRET" ]; then
    cat <<EOF | kubectl patch deployment "$workload" -n "$NS" --type=strategic --patch-file=/dev/stdin
spec:
  template:
    spec:
      imagePullSecrets:
        - name: ${PROBE_IMAGE_PULL_SECRET}
          \$patch: delete
EOF
  fi
  kubectl rollout status deployment/"$workload" -n "$NS" \
    --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"
done

kubectl delete -f "$SCRIPT_DIR/deploy/podmonitor.yaml" --ignore-not-found
echo "[OK] Network probe sidecars and PodMonitor removed."
