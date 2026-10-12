#!/usr/bin/env bash
# Shared helpers for the 20 reconstructed KubeIn paper scenarios.

set -euo pipefail

NS="${KUBEIN_SCENARIO_NAMESPACE:-sock-shop}"
SCENARIO_STATE_DIR="${SCENARIO_STATE_DIR:-}"

log() { printf '[scenario] %s\n' "$*"; }
die() { printf '[scenario:ERROR] %s\n' "$*" >&2; exit 1; }

init_state() {
  local scenario_dir="$1"
  if [ -z "$SCENARIO_STATE_DIR" ]; then
    SCENARIO_STATE_DIR="$scenario_dir/.state"
  fi
  mkdir -p "$SCENARIO_STATE_DIR"
}

need_kubectl() {
  command -v kubectl >/dev/null 2>&1 || die 'kubectl을 찾을 수 없습니다.'
  kubectl get namespace "$NS" >/dev/null 2>&1 \
    || die "namespace를 찾을 수 없습니다: $NS"
}

need_deployment() {
  kubectl get deployment "$1" -n "$NS" >/dev/null 2>&1 \
    || die "Deployment를 찾을 수 없습니다: $NS/$1"
}

need_service() {
  kubectl get service "$1" -n "$NS" >/dev/null 2>&1 \
    || die "Service를 찾을 수 없습니다: $NS/$1"
}

first_container() {
  kubectl get deployment "$1" -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].name}'
}

save_replicas() {
  local deployment="$1"
  kubectl get deployment "$deployment" -n "$NS" \
    -o jsonpath='{.spec.replicas}' > "$SCENARIO_STATE_DIR/${deployment}.replicas"
}

restore_replicas() {
  local deployment="$1"
  local file="$SCENARIO_STATE_DIR/${deployment}.replicas"
  [ -f "$file" ] || die "복구할 replica 상태가 없습니다: $file"
  kubectl scale deployment "$deployment" -n "$NS" --replicas="$(cat "$file")"
}

save_deployment_template() {
  local deployment="$1"
  local file="$SCENARIO_STATE_DIR/${deployment}.template.json"
  [ -s "$file" ] && return 0
  kubectl get deployment "$deployment" -n "$NS" -o json \
    | python3 -c 'import json,sys
data=json.load(sys.stdin)
json.dump(data["spec"]["template"], sys.stdout, separators=(",", ":"))' \
    > "$file"
}

restore_deployment_template() {
  local deployment="$1"
  local file="$SCENARIO_STATE_DIR/${deployment}.template.json"
  local payload
  [ -s "$file" ] || die "복구할 Deployment template 상태가 없습니다: $file"
  payload="$(python3 -c 'import json,sys
with open(sys.argv[1], encoding="utf-8") as stream:
    template=json.load(stream)
print(json.dumps([{"op":"replace","path":"/spec/template","value":template}], separators=(",", ":")))' "$file")"
  kubectl patch deployment "$deployment" -n "$NS" --type=json -p "$payload"
}

save_service_selector() {
  local service="$1"
  kubectl get service "$service" -n "$NS" \
    -o jsonpath='{.spec.selector}' > "$SCENARIO_STATE_DIR/${service}.selector.json"
}

restore_service_selector() {
  local service="$1"
  local file="$SCENARIO_STATE_DIR/${service}.selector.json"
  local selector
  [ -f "$file" ] || die "복구할 selector 상태가 없습니다: $file"
  selector="$(cat "$file")"
  [ -n "$selector" ] || die "저장된 selector가 비어 있습니다: $file"
  kubectl patch service "$service" -n "$NS" --type=merge \
    -p "{\"spec\":{\"selector\":${selector}}}"
}

undo_rollout() {
  local deployment="$1"
  kubectl rollout undo deployment/"$deployment" -n "$NS"
}

duration_seconds() {
  local value="$1" remaining unit amount total=0

  if [[ "$value" =~ ^[0-9]+$ ]]; then
    [ "$value" -gt 0 ] || return 1
    printf '%s\n' "$value"
    return 0
  fi

  remaining="$value"
  while [[ "$remaining" =~ ^([0-9]+)(h|m|s)(.*)$ ]]; do
    amount="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    remaining="${BASH_REMATCH[3]}"
    case "$unit" in
      h) total=$((total + amount * 3600)) ;;
      m) total=$((total + amount * 60)) ;;
      s) total=$((total + amount)) ;;
    esac
  done
  [ -z "$remaining" ] && [ "$total" -gt 0 ] || return 1
  printf '%s\n' "$total"
}

kubectl_with_hard_timeout() {
  local timeout_seconds="$1"
  shift
  command -v timeout >/dev/null 2>&1 \
    || die 'rollout hard timeout에 필요한 timeout 명령을 찾을 수 없습니다.'
  # A Bash wrapper keeps mock functions usable in contract tests while the
  # production path still resolves the real kubectl executable from PATH.
  if declare -F kubectl >/dev/null 2>&1; then
    export -f kubectl
  fi
  # wait_rollout uses this only for read-only state queries.  SIGKILL closes
  # descendant-held capture pipes at the deadline instead of leaving a grace
  # window after the monitored shell has exited.
  timeout --signal=KILL "${timeout_seconds}s" \
    bash -c 'kubectl "$@"' bash "$@"
}

wait_rollout() {
  # Sock Shop catalogue has a 180-second readiness initial delay.  Leave
  # scheduling and probe margin so a successful recovery is not misclassified.
  # Do not use `kubectl rollout status`: a stale ProgressDeadlineExceeded
  # condition can make it fail immediately even while the current generation
  # is converging normally.
  local deployment="$1"
  local timeout_value="${ROLLOUT_TIMEOUT:-300s}"
  local poll_interval="${ROLLOUT_POLL_INTERVAL_SECONDS:-2}"
  local request_timeout_cap="${ROLLOUT_REQUEST_TIMEOUT_SECONDS:-5}"
  local timeout deadline remaining request_timeout sleep_for state
  local generation observed desired replicas updated ready available unavailable

  timeout="$(duration_seconds "$timeout_value")" \
    || die "invalid ROLLOUT_TIMEOUT: $timeout_value"
  [[ "$poll_interval" =~ ^[1-9][0-9]*$ ]] \
    || die "invalid ROLLOUT_POLL_INTERVAL_SECONDS: $poll_interval"
  [[ "$request_timeout_cap" =~ ^[1-9][0-9]*$ ]] \
    || die "invalid ROLLOUT_REQUEST_TIMEOUT_SECONDS: $request_timeout_cap"
  deadline=$((SECONDS + timeout))

  while true; do
    remaining=$((deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
      log "rollout convergence timeout: $NS/$deployment state=${state:-unavailable}"
      return 1
    fi
    request_timeout="$request_timeout_cap"
    [ "$request_timeout" -le "$remaining" ] \
      || request_timeout="$remaining"
    state=""
    if state="$(kubectl_with_hard_timeout "$request_timeout" \
      get deployment "$deployment" -n "$NS" \
      -o jsonpath='{.metadata.generation}{"|"}{.status.observedGeneration}{"|"}{.spec.replicas}{"|"}{.status.replicas}{"|"}{.status.updatedReplicas}{"|"}{.status.readyReplicas}{"|"}{.status.availableReplicas}{"|"}{.status.unavailableReplicas}{"\n"}' \
      --request-timeout="${request_timeout}s" \
      2>/dev/null)"; then
      generation=""; observed=""; desired=""; replicas=""
      updated=""; ready=""; available=""; unavailable=""
      IFS='|' read -r generation observed desired replicas updated ready available unavailable \
        <<< "$state"
      desired="${desired:-1}"
      replicas="${replicas:-0}"
      updated="${updated:-0}"
      ready="${ready:-0}"
      available="${available:-0}"
      unavailable="${unavailable:-0}"

      if [[ "$generation" =~ ^[0-9]+$ \
        && "$observed" =~ ^[0-9]+$ \
        && "$desired" =~ ^[0-9]+$ \
        && "$replicas" =~ ^[0-9]+$ \
        && "$updated" =~ ^[0-9]+$ \
        && "$ready" =~ ^[0-9]+$ \
        && "$available" =~ ^[0-9]+$ \
        && "$unavailable" =~ ^[0-9]+$ ]] \
        && [ "$observed" -ge "$generation" ] \
        && [ "$replicas" -eq "$desired" ] \
        && [ "$updated" -eq "$desired" ] \
        && [ "$ready" -eq "$desired" ] \
        && [ "$available" -eq "$desired" ] \
        && [ "$unavailable" -eq 0 ]; then
        log "rollout converged: $NS/$deployment generation=$generation replicas=$desired"
        return 0
      fi
    fi

    remaining=$((deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
      log "rollout convergence timeout: $NS/$deployment state=${state:-unavailable}"
      return 1
    fi
    sleep_for="$poll_interval"
    [ "$sleep_for" -le "$remaining" ] || sleep_for="$remaining"
    [ "$sleep_for" -eq 0 ] || sleep "$sleep_for"
  done
}

set_bad_image() {
  local deployment="$1" container
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  kubectl set image deployment/"$deployment" -n "$NS" \
    "${container}=registry.invalid/kubein/not-found:paper-scenario"
}

set_memory_limit() {
  local deployment="$1" limit="$2"
  save_deployment_template "$deployment"
  kubectl set resources deployment/"$deployment" -n "$NS" \
    --limits="memory=${limit}" \
    --requests="memory=${limit}"
}

set_failing_probe() {
  local deployment="$1" kind="$2" container payload
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  payload="{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"${container}\",\"${kind}Probe\":{\"httpGet\":{\"path\":\"/__kubein_${kind}_failure\",\"port\":8080},\"initialDelaySeconds\":1,\"periodSeconds\":2,\"timeoutSeconds\":1,\"failureThreshold\":1}}]}}}}"
  kubectl patch deployment "$deployment" -n "$NS" --type=strategic -p "$payload"
}

set_root_filesystem_writable_for_target() {
  local deployment="$1" target_label="$2" container payload
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  payload="{\"spec\":{\"template\":{\"metadata\":{\"labels\":{\"${target_label}\":\"true\"}},\"spec\":{\"containers\":[{\"name\":\"${container}\",\"securityContext\":{\"readOnlyRootFilesystem\":false}}]}}}}"
  kubectl patch deployment "$deployment" -n "$NS" --type=strategic -p "$payload"
}

set_root_filesystem_writable() {
  set_root_filesystem_writable_for_target "$1" kubein-dnschaos-target
}

add_bad_configmap_env() {
  local deployment="$1" configmap="$2" container payload
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  kubectl create configmap "$configmap" -n "$NS" \
    --from-literal=NODE_OPTIONS='--kubein-invalid-option' \
    --dry-run=client -o yaml | kubectl apply -f -
  payload="{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"${container}\",\"envFrom\":[{\"configMapRef\":{\"name\":\"${configmap}\"}}]}]}}}}"
  kubectl patch deployment "$deployment" -n "$NS" --type=strategic -p "$payload"
}

add_then_remove_secret_env() {
  local deployment="$1" secret="$2" container payload
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  kubectl create secret generic "$secret" -n "$NS" \
    --from-literal=KUBEIN_PROBE=present \
    --dry-run=client -o yaml | kubectl apply -f -
  payload="{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"${container}\",\"envFrom\":[{\"secretRef\":{\"name\":\"${secret}\"}}]}]}}}}"
  kubectl patch deployment "$deployment" -n "$NS" --type=strategic -p "$payload"
  wait_rollout "$deployment"
  kubectl delete secret "$secret" -n "$NS" --ignore-not-found
  kubectl delete pod -n "$NS" -l "name=${deployment}" --ignore-not-found --wait=false
}

add_missing_pvc_mount() {
  local deployment="$1" claim="$2" container payload
  save_deployment_template "$deployment"
  container="$(first_container "$deployment")"
  payload="{\"spec\":{\"template\":{\"spec\":{\"volumes\":[{\"name\":\"kubein-missing-pvc\",\"persistentVolumeClaim\":{\"claimName\":\"${claim}\"}}],\"containers\":[{\"name\":\"${container}\",\"volumeMounts\":[{\"name\":\"kubein-missing-pvc\",\"mountPath\":\"/kubein-missing-pvc\"}]}]}}}}"
  kubectl patch deployment "$deployment" -n "$NS" --type=strategic -p "$payload"
}

set_selector_mismatch() {
  local service="$1"
  kubectl patch service "$service" -n "$NS" --type=merge \
    -p '{"spec":{"selector":{"name":"kubein-selector-mismatch"}}}'
}

delete_pods_by_label() {
  kubectl delete pod -n "$NS" -l "name=$1" --ignore-not-found --wait=false
}
