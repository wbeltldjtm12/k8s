#!/bin/bash
# ablation-eval.sh — KubeIn 4-mode Ablation Study
set -uo pipefail

SCENARIO_DIR="$HOME/aiops/scenarios/sock-shop"
RESULT_DIR="$SCENARIO_DIR/results/ablation_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULT_DIR"

API_BASE="http://localhost:8000"
CACHE_API="$API_BASE/api/cache/invalidate"
EVAL_API="$API_BASE/api/evaluate"

SCENARIOS=(
  "01-pod-kill"
  "02-pod-failure"
  "02-pod-kill-ordersdb"
  "03-container-kill"
  "04-bad-image"
  "05-oomkill"
  "06-readiness-probe"
  "07-liveness-probe"
  "08-secret-delete"
  "09-bad-configmap"
  "10-bad-pvc"
  "11-dns-error"
  "12-network-delay"
  "13-network-loss"
  "14-cpu-stress"
  "15-io-delay"
  "16-selector-mismatch"
  "17-memory-stress"
  "18-cascade-carts"
  "19-cascade-secret"
  "20-multi-fault"
)

SKIP_DETECTION=(
  "01-pod-kill"
  "02-pod-kill-ordersdb"
  "03-container-kill"
  "07-liveness-probe"
  "11-dns-error"
  "12-network-delay"
  "13-network-loss"
  "14-cpu-stress"
  "15-io-delay"
  "17-memory-stress"
  "18-cascade-carts"
)

is_skip_scenario() {
  local s="$1"
  for skip in "${SKIP_DETECTION[@]}"; do
    [ "$s" == "$skip" ] && return 0
  done
  return 1
}

MODES=("hybrid" "dfs_only" "llm_only")
TOTAL=${#SCENARIOS[@]}

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

wait_ready() {
  local timeout=120
  local elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local not_ready
    not_ready=$(kubectl get pods -n sock-shop --no-headers 2>/dev/null \
      | grep -v "1/1\|2/2" | grep -v "Completed" | wc -l)
    if [ "$not_ready" -eq 0 ]; then
      echo -e "${GREEN}[OK] 모든 Pod Ready${NC}"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  echo -e "${YELLOW}[경고] ${timeout}초 초과, 일부 Pod 미준비${NC}"
}

force_clean_residual() {
  for dep in $(kubectl get deploy -n sock-shop --no-headers | awk '{print $1}'); do
    kubectl rollout restart deployment "$dep" -n sock-shop 2>/dev/null
  done
  kubectl rollout status deployment --all -n sock-shop --timeout=120s 2>/dev/null
  sleep 10
  kubectl get rs -n sock-shop --no-headers 2>/dev/null \
    | awk '$2+0==0{print $1}' \
    | xargs -r kubectl delete rs -n sock-shop 2>/dev/null
}

wait_baseline_zero() {
  local max_wait=18
  for try in $(seq 1 $max_wait); do
    curl -s -X POST "$CACHE_API" > /dev/null 2>&1
    sleep 2
    local cnt
    cnt=$(curl -s "$EVAL_API?mode=hybrid" 2>/dev/null \
      | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('error_node_count',0))" 2>/dev/null || echo "99")
    if [ "$cnt" -le 0 ] 2>/dev/null; then
      echo "  [baseline] error_node_count=0 (${try}번째 시도)"
      return 0
    fi
    echo "  [baseline 대기 ${try}/${max_wait}] error=${cnt}, 3초 후 재시도..."
    sleep 3
  done
  echo -e "  ${YELLOW}[경고] baseline이 0이 되지 않음, 현재값으로 진행${NC}"
  return 1
}

inject_scenario() {
  local dir="$SCENARIO_DIR/$1"
  if [ -f "$dir/inject.sh" ]; then
    bash "$dir/inject.sh"
  elif [ -f "$dir/fault.yaml" ]; then
    kubectl apply -f "$dir/fault.yaml"
  else
    echo -e "${RED}[에러] inject.sh / fault.yaml 없음: $dir${NC}"
    return 1
  fi
}

recover_scenario() {
  local dir="$SCENARIO_DIR/$1"
  if [ -f "$dir/recover.sh" ]; then
    bash "$dir/recover.sh"
  elif [ -f "$dir/fault.yaml" ]; then
    kubectl delete -f "$dir/fault.yaml" 2>/dev/null || true
  fi
}

wait_for_detection() {
  local max_tries=15
  local try=1
  while [ $try -le $max_tries ]; do
    sleep 2
    curl -s -X POST "$CACHE_API" > /dev/null 2>&1
    sleep 1
    local err_count
    err_count=$(curl -s "$EVAL_API?mode=hybrid" 2>/dev/null \
      | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('error_node_count',0))" 2>/dev/null || echo "0")
    if [ "$err_count" -gt 0 ] 2>/dev/null; then
      echo -e "  ${GREEN}[감지] error_nodes=${err_count} (시도 ${try})${NC}"
      return 0
    fi
    echo -e "  [시도 ${try}/$max_tries] 탐지 대기 중... (error=${err_count})"
    try=$((try + 1))
  done
  echo -e "  ${RED}[미탐지] 30초 내 에러 감지 실패${NC}"
  return 1
}

call_all_modes() {
  local scenario_name=$1
  local scenario_dir="$RESULT_DIR/$scenario_name"
  mkdir -p "$scenario_dir"

  curl -s -X POST "$CACHE_API" > /dev/null 2>&1
  sleep 1

  for mode in "${MODES[@]}"; do
    local response
    response=$(curl -s "$EVAL_API?mode=$mode" 2>/dev/null || echo "{}")
    echo "$response" > "$scenario_dir/${mode}.json"

    if [ "$mode" == "llm_only" ]; then
      local ai_len
      ai_len=$(echo "$response" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d.get('data',{}).get('ai_analysis','')))" 2>/dev/null || echo "0")
      if [ "$ai_len" -gt 10 ]; then
        echo -e "    ${CYAN}[$mode]${NC} ai_analysis 성공 (길이=${ai_len})"
      else
        echo -e "    ${CYAN}[$mode]${NC} ai_analysis 실패"
      fi
    else
      local chains
      chains=$(echo "$response" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('data',{}).get('total_chains',0))" 2>/dev/null || echo "0")
      echo -e "    ${CYAN}[$mode]${NC} total_chains=${chains}"
    fi
  done

  python3 -c "
import json
dfs_path = '$scenario_dir/dfs_only.json'
out_path = '$scenario_dir/dfs_template.json'
with open(dfs_path) as f:
    d = json.load(f)
chains = d.get('data',{}).get('chains',[])
if not chains:
    tpl = '(결과 없음)'
else:
    parts = []
    for i, c in enumerate(chains[:3], 1):
        parts.append(f'''[체인 {i}]
- Root Cause: {c.get('root_cause_kind','')}/{c.get('root_cause','')}
- 상태: {c.get('root_cause_reason','')}
- 영향 범위: {c.get('blast_radius',0)}개 리소스
- 전파 깊이: {c.get('depth',0)}
- 체인 경로: {c.get('chain_summary','N/A')}
- 권장 조치: kubectl describe {c.get('root_cause_kind','').lower()} {c.get('root_cause','')} -n sock-shop''')
    tpl = chr(10).join([chr(10).join([]) if False else ''] + parts).strip()
    tpl = (chr(10)+chr(10)).join(parts)
d['data']['ai_analysis'] = tpl
d['mode'] = 'dfs_template'
with open(out_path, 'w') as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
print(f'    [dfs_template] 생성 완료 (길이={len(tpl)})')
" 2>/dev/null
}

echo "=========================================="
echo " KubeIn Ablation Study (${TOTAL} scenarios x 4 modes)"
echo " 결과 경로: $RESULT_DIR"
echo "=========================================="
echo "$RESULT_DIR" > "$SCENARIO_DIR/results/latest_ablation_dir.txt"

for i in "${!SCENARIOS[@]}"; do
  scenario="${SCENARIOS[$i]}"
  num=$((i + 1))
  echo ""
  echo "=========================================="
  echo " [${num}/${TOTAL}] — ${scenario}"
  echo "=========================================="

  wait_ready
  force_clean_residual
  wait_ready
  wait_baseline_zero

  echo -e "${YELLOW}[에러 주입] ${scenario}${NC}"
  inject_scenario "$scenario"
  echo "  [대기] 5초..."
  sleep 5

  if is_skip_scenario "$scenario"; then
    echo -e "  ${YELLOW}[스킵] detectable=false — 탐지 대기 건너뜀${NC}"
    curl -s -X POST "$CACHE_API" > /dev/null 2>&1
    sleep 1
  else
    wait_for_detection
  fi

  echo "  [4모드 평가 시작]"
  call_all_modes "$scenario"

  echo -e "${YELLOW}[시스템 복구] ${scenario}${NC}"
  recover_scenario "$scenario"
  echo "  [대기] 25초..."
  sleep 25
done

echo ""
echo "=========================================="
echo " Ablation Study 전체 완료! 결과: $RESULT_DIR"
echo "=========================================="
