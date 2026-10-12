#!/usr/bin/env bash
# 03-pre-check-cluster.sh
# 클러스터 환경에서 KubeIn 실험 직전 사전 점검
# 변경점 vs 단일노드 pre-check.sh:
#   - SCENARIO_DIR: environment override (default: repository scenario bundle)
#   - API_BASE: 환경변수로 주입 (기본값 localhost:8000)
#   - EXPECTED_NODE_COUNT/REQUIRE_CHAOS_MESH로 실행 profile 지정

set -uo pipefail

# ── 경로 설정 (환경변수로 오버라이드 가능) ─────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCENARIO_DIR="${SCENARIO_DIR:-$REPO_ROOT/cluster-port/sock-shop-scenarios}"
API_BASE="${API_BASE:-http://localhost:8000}"
CACHE_API="$API_BASE/api/cache/invalidate"
EVAL_API="$API_BASE/api/evaluate"
HEALTH_API="$API_BASE/api/health"
LLM_HEALTH_API="$API_BASE/api/health/llm"
NS="sock-shop"
EXPECTED_NODE_COUNT="${EXPECTED_NODE_COUNT:-3}"
EXPECTED_DETECTABLE_COUNT="${EXPECTED_DETECTABLE_COUNT:-11}"
REQUIRE_CHAOS_MESH="${REQUIRE_CHAOS_MESH:-true}"
LLM_PREFLIGHT="${LLM_PREFLIGHT:-true}"
GROUND_TRUTH_FILE="${GROUND_TRUTH_FILE:-}"
SCENARIO_MANIFEST="${SCENARIO_MANIFEST:-$REPO_ROOT/eval/scenarios.txt}"
SCENARIO_METADATA_FILE="${SCENARIO_METADATA_FILE:-$SCENARIO_DIR/scenario-metadata.json}"
SCENARIO_METADATA_VALIDATOR="${SCENARIO_METADATA_VALIDATOR:-$REPO_ROOT/eval/validate-scenario-metadata.py}"

if [ ! -f "$SCENARIO_MANIFEST" ]; then
  echo "[오류] scenario manifest를 찾을 수 없음: $SCENARIO_MANIFEST" >&2
  exit 2
fi
mapfile -t SCENARIOS < <(
  sed -e 's/\r$//' -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$SCENARIO_MANIFEST"
)
if [ "${#SCENARIOS[@]}" -ne 20 ] \
  || [ "$(printf '%s\n' "${SCENARIOS[@]}" | sort -u | wc -l | tr -d ' ')" -ne 20 ]; then
  echo "[오류] scenario manifest는 고유한 20개 항목이어야 함: $SCENARIO_MANIFEST" >&2
  exit 2
fi

pass_count=0; fail_count=0; warn_count=0
ok()   { echo "[OK]   $*"; pass_count=$((pass_count+1)); }
fail() { echo "[FAIL] $*"; fail_count=$((fail_count+1)); }
warn() { echo "[경고] $*"; warn_count=$((warn_count+1)); }

echo "================================================="
echo " KubeIn 클러스터 사전 점검"
echo " SCENARIO_DIR: $SCENARIO_DIR"
echo " METADATA    : $SCENARIO_METADATA_FILE"
echo " API_BASE    : $API_BASE"
echo " EXPECTED_NODE_COUNT: $EXPECTED_NODE_COUNT"
echo "================================================="

# 0) 시나리오별 관측 settle metadata
if [ ! -f "$SCENARIO_METADATA_VALIDATOR" ]; then
  fail "scenario metadata validator를 찾을 수 없음: $SCENARIO_METADATA_VALIDATOR"
elif [ ! -f "$SCENARIO_METADATA_FILE" ]; then
  fail "scenario metadata를 찾을 수 없음: $SCENARIO_METADATA_FILE"
elif METADATA_REPORT="$(
  python3 "$SCENARIO_METADATA_VALIDATOR" \
    --metadata "$SCENARIO_METADATA_FILE" \
    --manifest "$SCENARIO_MANIFEST" 2>&1
)"; then
  METADATA_COUNT="$(printf '%s\n' "$METADATA_REPORT" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$METADATA_COUNT" -eq "${#SCENARIOS[@]}" ]; then
    ok "scenario metadata ${METADATA_COUNT}/${#SCENARIOS[@]} strict resolve"
  else
    fail "scenario metadata resolve 수 불일치: ${METADATA_COUNT}/${#SCENARIOS[@]}"
  fi
else
  fail "scenario metadata 검증 실패: $METADATA_REPORT"
fi

# 1) kubectl 연결 + 노드 수 확인
NODE_COUNT="$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
READY_COUNT="$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/ {count++} END {print count+0}')"
if ! [[ "$EXPECTED_NODE_COUNT" =~ ^[1-9][0-9]*$ ]]; then
  fail "EXPECTED_NODE_COUNT는 1 이상의 정수여야 함: $EXPECTED_NODE_COUNT"
elif [ "${NODE_COUNT:-0}" -lt "$EXPECTED_NODE_COUNT" ]; then
  fail "클러스터 노드 ${NODE_COUNT:-0}개 (필요: ${EXPECTED_NODE_COUNT}개)"
elif [ "${READY_COUNT:-0}" -ne "${NODE_COUNT:-0}" ]; then
  fail "일부 노드 NotReady (전체: ${NODE_COUNT:-0}, Ready: ${READY_COUNT:-0})"
else
  ok "클러스터 노드 ${NODE_COUNT}개 (Ready: ${READY_COUNT})"
  kubectl get nodes -o wide 2>/dev/null | head -10
fi

# 2) sock-shop Pod 상태
BAD_PODS="$(kubectl get pods -n "$NS" --no-headers 2>/dev/null \
  | awk '$3 == "Terminating" {next} {n=split($2, ready, "/"); if (n != 2 || ready[2] == 0 || ready[1] != ready[2] || $3 != "Running") print $1}')"
POD_COUNT="$(kubectl get pods -n "$NS" --no-headers 2>/dev/null | wc -l | tr -d ' ')"

if [ -z "${BAD_PODS:-}" ] && [ "${POD_COUNT:-0}" -ge 1 ]; then
  ok "sock-shop 안정 상태 (${POD_COUNT} pods)"
else
  fail "sock-shop Pod 준비 실패 (bad: ${BAD_PODS:-없음})"
fi

# 3) Chaos Mesh 설치 확인
CHAOS_POD_COUNT="$(kubectl get pods -n chaos-mesh --no-headers 2>/dev/null | wc -l | tr -d ' ')"
CHAOS_RUNNING_COUNT="$(kubectl get pods -n chaos-mesh --no-headers 2>/dev/null \
  | awk '$3 == "Running" {count++} END {print count+0}')"
CHAOS_BAD_PODS="$(kubectl get pods -n chaos-mesh --no-headers 2>/dev/null \
  | awk '$3 != "Completed" {n=split($2, ready, "/"); if (n != 2 || ready[2] == 0 || ready[1] != ready[2] || $3 != "Running") print $1}')"
if [ "${CHAOS_RUNNING_COUNT:-0}" -ge 1 ] && [ -z "${CHAOS_BAD_PODS:-}" ]; then
  ok "Chaos Mesh 전체 실행 Pod 준비 완료 (${CHAOS_RUNNING_COUNT}/${CHAOS_POD_COUNT} pods)"
elif [ "$REQUIRE_CHAOS_MESH" = "true" ]; then
  fail "Chaos Mesh 미설치 또는 준비 실패 (bad: ${CHAOS_BAD_PODS:-없음})"
else
  warn "Chaos Mesh 확인 생략 profile (Pod 수: ${CHAOS_POD_COUNT:-0})"
fi

# 4) 백엔드 API
check_chaos_residue() {
  local resource
  for resource in podchaos networkchaos dnschaos iochaos stresschaos podiochaos; do
    kubectl get "$resource" -n "$NS" -o name 2>/dev/null || true
  done
}

CHAOS_RESIDUE="$(check_chaos_residue)"
if [ -z "$CHAOS_RESIDUE" ]; then
  ok "sock-shop namespace에 잔류 Chaos 실험 없음"
else
  fail "잔류 Chaos 실험 발견: $(printf '%s' "$CHAOS_RESIDUE" | tr '\n' ' ')"
fi

EXPERIMENTAL_OBJECTS="$(
  kubectl get configmap,secret,pvc -n "$NS" -o name 2>/dev/null \
    | grep -E '(^|/)kubein-' || true
)"
DEPLOYMENT_MARKERS="$(
  kubectl get deployments -n "$NS" -o json 2>/dev/null \
    | grep -Eo '__kubein_[A-Za-z0-9_-]*|registry\.invalid/kubein[^" ]*|kubein-(missing-pvc|front-end-bad-config|catalogue-db[^" ]*secret)' \
    | sort -u || true
)"
if [ -z "$EXPERIMENTAL_OBJECTS" ] && [ -z "$DEPLOYMENT_MARKERS" ]; then
  ok "수동 장애 주입 잔류 리소스/Deployment marker 없음"
else
  fail "수동 장애 주입 잔류 발견: objects=${EXPERIMENTAL_OBJECTS:-none}, markers=${DEPLOYMENT_MARKERS:-none}"
fi

SHIPPING_READ_ONLY_ROOTFS="$(
  kubectl get deployment shipping -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}' \
    2>/dev/null || true
)"
if [ "$SHIPPING_READ_ONLY_ROOTFS" = "true" ]; then
  ok "shipping readOnlyRootFilesystem baseline=true"
else
  fail "shipping DNSChaos 준비 상태가 잔류함: readOnlyRootFilesystem=${SHIPPING_READ_ONLY_ROOTFS:-missing}"
fi

CARTS_DB_READ_ONLY_ROOTFS="$(
  kubectl get deployment carts-db -n "$NS" \
    -o jsonpath='{.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem}' \
    2>/dev/null || true
)"
if [ "$CARTS_DB_READ_ONLY_ROOTFS" = "true" ]; then
  ok "carts-db readOnlyRootFilesystem baseline=true"
else
  fail "carts-db IOChaos 준비 상태가 잔류함: readOnlyRootFilesystem=${CARTS_DB_READ_ONLY_ROOTFS:-missing}"
fi

DNS_TARGET_RESIDUE="$(
  kubectl get pods -n "$NS" -l kubein-dnschaos-target=true -o name \
    2>/dev/null || true
)"
if [ -z "$DNS_TARGET_RESIDUE" ]; then
  ok "DNSChaos 준비용 target Pod label 잔류 없음"
else
  fail "DNSChaos 준비용 target Pod 잔류: $DNS_TARGET_RESIDUE"
fi

IO_TARGET_RESIDUE="$(
  kubectl get pods -n "$NS" -l kubein-iochaos-target=true -o name \
    2>/dev/null || true
)"
if [ -z "$IO_TARGET_RESIDUE" ]; then
  ok "IOChaos 준비용 target Pod label 잔류 없음"
else
  fail "IOChaos 준비용 target Pod 잔류: $IO_TARGET_RESIDUE"
fi

if HEALTH_RESPONSE="$(curl -fsS --connect-timeout 5 --max-time 15 "$HEALTH_API")" \
  && printf '%s' "$HEALTH_RESPONSE" | python3 -c 'import json,sys
d=json.load(sys.stdin)
if d.get("kubernetes",{}).get("status") != "connected": raise SystemExit(1)
if d.get("chromadb",{}).get("status") != "ready": raise SystemExit(1)' 2>/dev/null; then
  ok "백엔드 API/Kubernetes/ChromaDB 정상 ($API_BASE)"
else
  fail "백엔드 health 실패 또는 Kubernetes/ChromaDB 미준비 ($API_BASE)"
fi

# 5) baseline error_node_count
if [ "$LLM_PREFLIGHT" = "true" ]; then
  if LLM_HEALTH_RESPONSE="$(curl -fsS --connect-timeout 10 --max-time 120 "$LLM_HEALTH_API")" \
    && printf '%s' "$LLM_HEALTH_RESPONSE" | python3 -c 'import json,sys
d=json.load(sys.stdin)
if d.get("status") != "ready": raise SystemExit(1)
if not isinstance(d.get("model"), str) or not d["model"].strip(): raise SystemExit(1)' 2>/dev/null; then
    ok "LLM 연결/auth/model 사전 점검 통과"
  else
    fail "LLM 연결/auth/model 사전 점검 실패 ($LLM_HEALTH_API)"
  fi
elif [ "$LLM_PREFLIGHT" = "false" ]; then
  warn "LLM preflight 비활성화"
else
  fail "LLM_PREFLIGHT는 true 또는 false여야 함: $LLM_PREFLIGHT"
fi

if ! curl -fsS --connect-timeout 5 --max-time 15 -X POST "$CACHE_API" >/dev/null; then
  fail "baseline cache 무효화 API 실패"
elif ! BASELINE_RESPONSE="$(curl -fsS --connect-timeout 5 --max-time 60 \
  --get --data-urlencode 'mode=dfs_only' "$EVAL_API")"; then
  fail "baseline 평가 API 실패"
elif ! BASELINE_ERR="$(printf '%s' "$BASELINE_RESPONSE" | python3 -c 'import sys,json
d=json.load(sys.stdin)
if d.get("status") != "success": raise ValueError("status")
v=d.get("error_node_count")
if not isinstance(v, int) or isinstance(v, bool): raise TypeError("error_node_count")
print(v)' 2>/dev/null)"; then
  fail "baseline 평가 응답 검증 실패"
elif [ "$BASELINE_ERR" -eq 0 ]; then
  ok "baseline error_node_count=0 (dfs_only)"
else
  fail "baseline error_node_count=${BASELINE_ERR} (0이어야 실험 가능)"
fi

# 6) Ground Truth 무결성
if [ -z "$GROUND_TRUTH_FILE" ]; then
  for candidate in \
    "$SCENARIO_DIR/eval/ground_truth.json" \
    "$SCENARIO_DIR/ground_truth.json" \
    "$SCENARIO_DIR/ground_truth.json.txt" \
    "$REPO_ROOT/eval/ground_truth.json"; do
    if [ -f "$candidate" ]; then
      GROUND_TRUTH_FILE="$candidate"
      break
    fi
  done
fi

if [ -z "$GROUND_TRUTH_FILE" ] || [ ! -f "$GROUND_TRUTH_FILE" ]; then
  fail "ground_truth.json을 찾을 수 없음 (GROUND_TRUTH_FILE로 지정 가능)"
else
  GT_REPORT="$(python3 - "$GROUND_TRUTH_FILE" "$EXPECTED_DETECTABLE_COUNT" "${SCENARIOS[@]}" <<'PY'
import json
import sys

path, expected_detectable_text, *active = sys.argv[1:]
try:
    expected_detectable_count = int(expected_detectable_text)
    if expected_detectable_count < 0:
        raise ValueError("expected detectable count must be non-negative")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise TypeError("top-level JSON must be an object")
    keys = set(data)
    missing = sorted(set(active) - keys)
    extra = sorted(keys - set(active))
    schema_errors = []
    detectable_count = 0
    for name in active:
        value = data.get(name)
        if not isinstance(value, dict):
            schema_errors.append(f"{name}:not-object")
            continue
        if not isinstance(value.get("detectable"), bool):
            schema_errors.append(f"{name}:detectable")
            continue
        detectable_count += int(value["detectable"])
        if value["detectable"]:
            kind = value.get("expected_rc_kind")
            target = value.get("expected_rc_name_contains")
            if not isinstance(kind, (str, list)) or not isinstance(target, (str, list)):
                schema_errors.append(f"{name}:expected-rc")
        if not isinstance(value.get("expected_affected", []), list):
            schema_errors.append(f"{name}:expected-affected")
    print(
        f"{len(data)}|{','.join(missing)}|{','.join(extra)}|"
        f"{detectable_count}|{','.join(schema_errors)}"
    )
    if missing:
        raise SystemExit(1)
    if schema_errors or detectable_count != expected_detectable_count:
        raise SystemExit(3)
    raise SystemExit(0)
except Exception as exc:
    print(f"ERR|{type(exc).__name__}: {exc}|||")
    raise SystemExit(2)
PY
)"
  GT_STATUS=$?
  IFS='|' read -r GT_COUNT GT_MISSING GT_EXTRA GT_DETECTABLE GT_SCHEMA <<< "$GT_REPORT"
  if [ "$GT_STATUS" -eq 0 ]; then
    ok "Ground Truth active 20개 / detectable=$EXPECTED_DETECTABLE_COUNT / 스키마 확인 ($GROUND_TRUTH_FILE)"
    [ -n "${GT_EXTRA:-}" ] && warn "Ground Truth legacy/extra key: $GT_EXTRA"
  elif [ "$GT_STATUS" -eq 1 ]; then
    fail "Ground Truth active key 누락: ${GT_MISSING:-알 수 없음}"
  elif [ "$GT_STATUS" -eq 3 ]; then
    fail "Ground Truth 정책/스키마 불일치 (detectable=${GT_DETECTABLE:-?}, errors=${GT_SCHEMA:-없음})"
  else
    fail "Ground Truth 파싱 실패: ${GT_MISSING:-$GT_REPORT}"
  fi
fi

# 7) 시나리오 파일 존재 확인
FOUND=0
for scen in "${SCENARIOS[@]}"; do
  if [ -d "$SCENARIO_DIR/$scen" ]; then
    FOUND=$((FOUND+1))
    if [ ! -f "$SCENARIO_DIR/$scen/inject.sh" ] && [ ! -f "$SCENARIO_DIR/$scen/fault.yaml" ]; then
      fail "$scen: inject.sh / fault.yaml 없음"
    fi
    if [ -f "$SCENARIO_DIR/$scen/inject.sh" ] && grep -qi 'SKIP' "$SCENARIO_DIR/$scen/inject.sh"; then
      warn "$scen: inject.sh가 SKIP 시나리오로 표시됨"
    fi
  fi
done

if [ "$FOUND" -eq 20 ]; then
  ok "시나리오 디렉터리 20/20"
else
  fail "시나리오 디렉터리 누락 (${FOUND}/20) — SCENARIO_DIR=$SCENARIO_DIR"
fi

echo ""
echo "================================================="
echo " 결과: ${pass_count} 통과 / ${fail_count} 실패 / ${warn_count} 경고"
echo "================================================="

[ "$fail_count" -gt 0 ] && exit 1 || exit 0
