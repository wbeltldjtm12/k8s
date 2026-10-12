#!/usr/bin/env bash
# Add a lightweight network-probe sidecar to the three workloads used by
# scenarios 11 (DNS), 12 (delay), and 13 (loss).  It is lab instrumentation,
# not part of the published agent-free KubeIn baseline.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS="${KUBEIN_SCENARIO_NAMESPACE:-sock-shop}"
PROBE_IMAGE="${PROBE_IMAGE:?Set PROBE_IMAGE, e.g. sch02/kubein-network-probe:v1}"
PROBE_IMAGE_PULL_SECRET="${PROBE_IMAGE_PULL_SECRET:-}"
ROLLOUT_TIMEOUT_SECONDS="${PROBE_ROLLOUT_TIMEOUT_SECONDS:-360}"

require_deployment() {
  kubectl get deployment "$1" -n "$NS" >/dev/null
}

patch_workload() {
  local workload="$1" dns_targets="$2" http_targets="$3"
  local probe_type="$4" probe_target="$5"
  local image_pull_secret_block=""
  if [ -n "$PROBE_IMAGE_PULL_SECRET" ]; then
    printf -v image_pull_secret_block \
      '      imagePullSecrets:\n        - name: %s\n' \
      "$PROBE_IMAGE_PULL_SECRET"
  fi
  cat <<EOF | kubectl patch deployment "$workload" -n "$NS" --type=strategic --patch-file=/dev/stdin
spec:
  template:
    metadata:
      labels:
        kubein.io/network-probe: "true"
        kubein.io/network-probe-type: "${probe_type}"
        kubein.io/network-probe-target: "${probe_target}"
    spec:
${image_pull_secret_block}      containers:
        - name: kubein-netprobe
          image: ${PROBE_IMAGE}
          imagePullPolicy: Always
          ports:
            - name: netprobe
              containerPort: 9155
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          resources:
            requests:
              cpu: 5m
              memory: 24Mi
            limits:
              cpu: 100m
              memory: 64Mi
          env:
            - name: PROBE_SOURCE_WORKLOAD
              value: ${workload}
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            - name: DNS_TARGETS
              value: ${dns_targets}
            - name: HTTP_TARGETS
              value: ${http_targets}
            - name: PROBE_INTERVAL_SECONDS
              value: "2"
            - name: PROBE_TIMEOUT_SECONDS
              value: "2"
EOF
  kubectl rollout status deployment/"$workload" -n "$NS" \
    --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"
}

if [[ ! "$ROLLOUT_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] PROBE_ROLLOUT_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi
for workload in shipping payment front-end; do
  require_deployment "$workload"
done
if [ -n "$PROBE_IMAGE_PULL_SECRET" ]; then
  if [ "${#PROBE_IMAGE_PULL_SECRET}" -gt 253 ] || \
    [[ ! "$PROBE_IMAGE_PULL_SECRET" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo "[ERROR] Invalid PROBE_IMAGE_PULL_SECRET name." >&2
    exit 2
  fi
  kubectl get secret "$PROBE_IMAGE_PULL_SECRET" -n "$NS" >/dev/null
fi

patch_workload \
  shipping \
  "catalogue=catalogue.sock-shop.svc.cluster.local" \
  "" \
  "dns" \
  "catalogue"
patch_workload \
  payment \
  "" \
  "catalogue=http://catalogue.sock-shop.svc.cluster.local/health" \
  "http" \
  "catalogue"
patch_workload \
  front-end \
  "" \
  "front-end=http://front-end.sock-shop.svc.cluster.local/" \
  "http" \
  "front-end"

kubectl apply -f "$SCRIPT_DIR/deploy/podmonitor.yaml"
echo "[OK] Network probe sidecars and PodMonitor applied."
