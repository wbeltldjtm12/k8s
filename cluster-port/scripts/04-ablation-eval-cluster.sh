#!/usr/bin/env bash
# 04-ablation-eval-cluster.sh
# KubeIn canonical ablation 평가 스크립트
#
#   - SCENARIO_DIR/API_BASE 환경변수 지원
#   - 한 번 캡처한 evaluation snapshot을 3개 mode가 공유
#   - API/JSON/snapshot 검증 실패를 invalid 결과와 exit code로 전파

set -uo pipefail

# ── 환경변수 설정 (오버라이드 가능) ───────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCENARIO_DIR="${SCENARIO_DIR:-$HOME/kubein/sock-shop}"
API_BASE="${API_BASE:-http://localhost:8000}"
RESULTS_ROOT="${SCENARIO_DIR}/results"
CACHE_API="$API_BASE/api/cache/invalidate"
EVAL_API="$API_BASE/api/evaluate"
SNAPSHOT_API="$API_BASE/api/evaluate/snapshots"
NS="sock-shop"
EVAL_TIMEOUT_SECONDS="${EVAL_TIMEOUT_SECONDS:-240}"
RUN_FAILURES=0
ACTIVE_SNAPSHOT_ID=""
GROUND_TRUTH_FILE="${GROUND_TRUTH_FILE:-}"
EXCLUDED_SCENARIOS_CSV="${EXCLUDED_SCENARIOS_CSV-02-pod-failure,14-cpu-stress,17-memory-stress}"
SCENARIO_MANIFEST="${SCENARIO_MANIFEST:-$REPO_ROOT/eval/scenarios.txt}"

mkdir -p "$RESULTS_ROOT" || exit 2
RESULT_DIR="$(mktemp -d "$RESULTS_ROOT/ablation_$(date +%Y%m%d_%H%M%S)_XXXXXX")" || exit 2
LATEST_POINTER="$RESULTS_ROOT/latest_ablation_dir.txt"

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
IFS=',' read -r -a EXCLUDED_SCENARIOS <<< "$EXCLUDED_SCENARIOS_CSV"
MODES=("hybrid" "dfs_only" "llm_only")

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
  echo "[오류] ground_truth.json을 찾을 수 없습니다." >&2
  exit 2
fi
if ! GT_DETECTABLE_TEXT="$(python3 - "$GROUND_TRUTH_FILE" "${SCENARIOS[@]}" <<'PY'
import json
import sys

path, *active = sys.argv[1:]
with open(path, "r", encoding="utf-8") as stream:
    data = json.load(stream)
if not isinstance(data, dict) or any(name not in data for name in active):
    raise SystemExit("invalid or incomplete ground truth")
for name in active:
    value = data[name]
    if not isinstance(value, dict) or not isinstance(value.get("detectable"), bool):
        raise SystemExit(f"invalid ground truth entry: {name}")
    if value["detectable"]:
        print(name)
PY
)"; then
  echo "[오류] Ground Truth 스키마 검증 실패: $GROUND_TRUTH_FILE" >&2
  exit 2
fi
mapfile -t DETECTABLE_SCENARIOS <<< "$GT_DETECTABLE_TEXT"

echo "=========================================="
echo " KubeIn Ablation Study [클러스터 모드]"
echo " SCENARIO_DIR: $SCENARIO_DIR"
echo " API_BASE    : $API_BASE"
echo " 결과 경로   : $RESULT_DIR"
echo "=========================================="

log() { echo "$*"; }

release_active_snapshot() {
  local snapshot_id="${ACTIVE_SNAPSHOT_ID:-}"
  [ -z "$snapshot_id" ] && return 0
  ACTIVE_SNAPSHOT_ID=""
  curl -fsS --connect-timeout 10 --max-time 30 \
    -X DELETE "$SNAPSHOT_API/$snapshot_id" >/dev/null
}

cleanup_on_exit() {
  release_active_snapshot || true
}

trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

invalidate_cache() {
  curl -fsS --connect-timeout 10 --max-time 30 -X POST "$CACHE_API" >/dev/null
}

get_eval_json() {
  curl -fsS --connect-timeout 10 --max-time "$EVAL_TIMEOUT_SECONDS" \
    --get --data-urlencode "mode=${1:-dfs_only}" "$EVAL_API"
}

get_error_count() {
  local response count
  response="$(get_eval_json dfs_only)" || return 1
  count="$(printf '%s' "$response" | python3 -c 'import sys,json
d=json.load(sys.stdin)
if d.get("status") != "success": raise ValueError("API status is not success")
value=d.get("error_node_count")
if not isinstance(value, int) or isinstance(value, bool): raise TypeError("invalid error_node_count")
print(value)')" || return 1
  printf '%s\n' "$count"
}

get_top_chain_fields() {
  get_eval_json dfs_only | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    chains=d.get("data",{}).get("chains",[])
    if not chains:
        print(""); print(""); print(""); print("")
    else:
        c=chains[0]
        print(c.get("root_cause_kind",""))
        print(c.get("root_cause",""))
        print(c.get("root_cause_reason",""))
        print(c.get("score",""))
except Exception:
    print(""); print(""); print(""); print("")'
}

is_in_list() {
  local target="$1"; shift
  for x in "$@"; do [ "$x" = "$target" ] && return 0; done
  return 1
}

is_gt_detectable() {
  is_in_list "$1" "${DETECTABLE_SCENARIOS[@]}"
}

delete_zero_rs() {
  kubectl get rs -n "$NS" --no-headers 2>/dev/null \
    | awk '$2+0==0{print $1}' \
    | xargs -r kubectl delete rs -n "$NS" >/dev/null 2>&1 || true
}

wait_cluster_stable() {
  log "  [대기] deployment / pod 안정화 확인"
  kubectl wait --for=condition=Available deployment --all -n "$NS" --timeout=180s >/dev/null 2>&1 || true
  kubectl wait --for=condition=Ready pod --all -n "$NS" --timeout=180s >/dev/null 2>&1 || true

  local bad
  bad="$(kubectl get pods -n "$NS" --no-headers 2>/dev/null \
    | awk '{n=split($2, ready, "/"); if (n != 2 || ready[2] == 0 || ready[1] != ready[2] || $3 != "Running") print $1}')"

  if [ -n "${bad:-}" ]; then
    log "  [경고] 비정상 pod: $(echo "$bad" | tr '\n' ' ')"
    return 1
  fi
  log "  [OK] 클러스터 안정"
  return 0
}

cleanup_residue_aggressive() {
  local kind name reason score
  mapfile -t FIELDS < <(get_top_chain_fields)
  kind="${FIELDS[0]:-}"; name="${FIELDS[1]:-}"
  reason="${FIELDS[2]:-}"; score="${FIELDS[3]:-}"

  log "  [잔여 분석] kind=${kind:-N/A} name=${name:-N/A} reason=${reason:-N/A}"

  if [ "$kind" = "Pod" ] && [ "$reason" = "HighRestartCount" ] && [ -n "$name" ]; then
    log "  [자가치유] HighRestartCount Pod 삭제: $name"
    kubectl delete pod "$name" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
    kubectl wait --for=condition=Ready pod --all -n "$NS" --timeout=180s >/dev/null 2>&1 || true
    invalidate_cache || true
    sleep 5
    return 0
  fi
  return 1
}

wait_baseline_zero() {
  local max=18 i cnt
  for i in $(seq 1 "$max"); do
    if ! invalidate_cache; then
      log "  [baseline $i/$max] 캐시 무효화 API 실패"
      sleep 3
      continue
    fi
    sleep 2
    if ! cnt="$(get_error_count)"; then
      log "  [baseline $i/$max] 평가 API/JSON 실패"
      sleep 3
      continue
    fi
    [ "$cnt" -eq 0 ] && log "  [OK] baseline=0" && return 0
    log "  [baseline $i/$max] error=$cnt"
    cleanup_residue_aggressive && sleep 3 && continue
    sleep 3
  done
  log "  [실패] baseline이 0 안 됨"
  return 1
}

inject_scenario() {
  local scen="$1" dir="$SCENARIO_DIR/$scen" outdir="$RESULT_DIR/$scen"
  mkdir -p "$outdir"
  log "[주입] $scen"
  if [ -f "$dir/inject.sh" ]; then
    bash "$dir/inject.sh" 2>&1 | tee "$outdir/inject.log"
    return "${PIPESTATUS[0]}"
  elif [ -f "$dir/fault.yaml" ]; then
    kubectl apply -f "$dir/fault.yaml" 2>&1 | tee "$outdir/inject.log"
    return "${PIPESTATUS[0]}"
  else
    log "  [실패] inject.sh / fault.yaml 없음"
    return 1
  fi
}

recover_scenario() {
  local scen="$1" dir="$SCENARIO_DIR/$scen" outdir="$RESULT_DIR/$scen"
  mkdir -p "$outdir"
  log "[복구] $scen"
  if [ -f "$dir/recover.sh" ]; then
    bash "$dir/recover.sh" 2>&1 | tee "$outdir/recover.log"
    return "${PIPESTATUS[0]}"
  elif [ -f "$dir/fault.yaml" ]; then
    kubectl delete -f "$dir/fault.yaml" --ignore-not-found 2>&1 | tee "$outdir/recover.log"
    return "${PIPESTATUS[0]}"
  else
    : > "$outdir/recover.log"; return 0
  fi
}

wait_for_detection() {
  local tries=15 t err successful_polls=0
  for t in $(seq 1 "$tries"); do
    if ! invalidate_cache; then
      log "  [$t/$tries] 캐시 무효화 API 실패"
      sleep 2
      continue
    fi
    sleep 1
    if ! err="$(get_error_count)"; then
      log "  [$t/$tries] 평가 API/JSON 실패"
      sleep 2
      continue
    fi
    successful_polls=$((successful_polls + 1))
    [ "$err" -gt 0 ] && log "  [감지] error_nodes=$err (시도 $t)" && return 0
    log "  [$t/$tries] 탐지 대기 (error=$err)"; sleep 2
  done
  if [ "$successful_polls" -eq 0 ]; then
    log "  [실패] 유효한 탐지 API 응답이 한 번도 없음"
    return 2
  fi
  log "  [미탐지] 유효한 응답은 있었지만 error_nodes=0"
  return 1
}

call_all_modes() {
  local scen="$1"
  local outdir="$RESULT_DIR/$scen"
  local snapshot_tmp="$outdir/_snapshot.json.tmp"
  local snapshot_id mode response_tmp validation chains ai_len error_count
  local failed=0

  mkdir -p "$outdir"
  log "  [동일 snapshot 3모드 평가]"

  if ! curl -fsS --connect-timeout 10 --max-time 120 \
    -X POST "$SNAPSHOT_API" -o "$snapshot_tmp"; then
    log "    [실패] 평가 snapshot 생성 API 오류"
    [ -f "$snapshot_tmp" ] && mv -f "$snapshot_tmp" "$outdir/_snapshot.failed.json"
    return 1
  fi

  if ! snapshot_id="$(python3 -c 'import json,sys
d=json.load(sys.stdin)
snapshot_id=d.get("snapshot_id", "")
if d.get("status") != "success" or len(snapshot_id) != 32:
    raise ValueError("invalid snapshot response")
int(snapshot_id, 16)
print(snapshot_id)' < "$snapshot_tmp")"; then
    log "    [실패] 평가 snapshot 응답 검증 실패"
    mv -f "$snapshot_tmp" "$outdir/_snapshot.failed.json"
    return 1
  fi
  ACTIVE_SNAPSHOT_ID="$snapshot_id"
  if ! mv -f "$snapshot_tmp" "$outdir/_snapshot.json"; then
    log "    [실패] snapshot 산출물 저장 실패"
    release_active_snapshot || true
    return 1
  fi
  log "    [snapshot] $snapshot_id"

  for mode in "${MODES[@]}"; do
    response_tmp="$outdir/${mode}.json.tmp"
    if ! curl -fsS --connect-timeout 10 --max-time "$EVAL_TIMEOUT_SECONDS" \
      --get \
      --data-urlencode "mode=$mode" \
      --data-urlencode "snapshot_id=$snapshot_id" \
      "$EVAL_API" -o "$response_tmp"; then
      log "    [$mode] 실패: HTTP/전송 오류"
      [ -f "$response_tmp" ] && mv -f "$response_tmp" "$outdir/${mode}.failed.json"
      failed=1
      continue
    fi

    if ! validation="$(python3 -c 'import json,sys
expected_mode, expected_snapshot = sys.argv[1:3]
d=json.load(sys.stdin)
if d.get("status") != "success": raise ValueError("status is not success")
if d.get("mode") != expected_mode: raise ValueError("mode mismatch")
if d.get("snapshot_id") != expected_snapshot: raise ValueError("snapshot mismatch")
data=d.get("data")
if not isinstance(data, dict): raise TypeError("data is not an object")
chains=data.get("total_chains", 0)
ai_text=data.get("ai_analysis", "") or ""
errors=d.get("error_node_count", 0)
if not isinstance(chains, int) or not isinstance(errors, int): raise TypeError("invalid count")
if expected_mode in {"hybrid", "llm_only"} and errors > 0:
    if data.get("ai_status") != "success": raise ValueError("LLM analysis failed")
    if not isinstance(ai_text, str) or not ai_text.strip(): raise ValueError("empty LLM analysis")
print(f"{chains}|{len(ai_text)}|{errors}")' "$mode" "$snapshot_id" < "$response_tmp")"; then
      log "    [$mode] 실패: JSON/status/snapshot 검증 오류"
      mv -f "$response_tmp" "$outdir/${mode}.failed.json"
      failed=1
      continue
    fi

    if ! mv -f "$response_tmp" "$outdir/${mode}.json"; then
      log "    [$mode] 실패: 산출물 저장 오류"
      failed=1
      continue
    fi
    IFS='|' read -r chains ai_len error_count <<< "$validation"
    if [ "$mode" = "llm_only" ]; then
      log "    [$mode] ai_analysis 길이=$ai_len, error_nodes=$error_count"
    else
      log "    [$mode] total_chains=$chains, error_nodes=$error_count"
    fi
  done

  if ! release_active_snapshot; then
    log "    [경고] snapshot release 실패 (TTL로 자동 정리됨): $snapshot_id"
  fi

  return "$failed"
}

write_status() {
  local scen="$1" status="$2" detectable="$3" detected="$4" reason="$5"
  local outdir="$RESULT_DIR/$scen"
  local status_tmp="$outdir/status.txt.tmp"
  mkdir -p "$outdir" || return 1
  if ! printf 'scenario=%s\nstatus=%s\ndetectable=%s\ndetected=%s\nreason=%s\n' \
    "$scen" "$status" "$detectable" "$detected" "$reason" > "$status_tmp"; then
    return 1
  fi
  mv -f "$status_tmp" "$outdir/status.txt" || return 1
  if [ ! -f "$RESULT_DIR/_summary.tsv" ]; then
    printf 'scenario\tstatus\tdetectable\tdetected\treason\n' > "$RESULT_DIR/_summary.tsv" || return 1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$scen" "$status" "$detectable" "$detected" "$reason" >> "$RESULT_DIR/_summary.tsv" || return 1
  if [ "$status" = "invalid" ]; then
    RUN_FAILURES=$((RUN_FAILURES + 1))
  fi
  return 0
}

record_status() {
  if ! write_status "$@"; then
    log "[실패] 상태 산출물을 저장할 수 없음: $1"
    exit 1
  fi
}

# ── 메인 루프 ──────────────────────────────────────────
for idx in "${!SCENARIOS[@]}"; do
  scen="${SCENARIOS[$idx]}"
  num=$((idx+1))
  mkdir -p "$RESULT_DIR/$scen" || exit 1
  detectable="false"
  is_gt_detectable "$scen" && detectable="true"

  echo; echo "=========================================="; echo " [$num/${#SCENARIOS[@]}] — $scen"; echo "=========================================="

  if ! wait_cluster_stable; then
    record_status "$scen" "invalid" "$detectable" "not_run" "cluster not stable before injection"
    continue
  fi
  delete_zero_rs

  if ! wait_baseline_zero; then
    record_status "$scen" "invalid" "$detectable" "not_run" "baseline not zero before injection"
    continue
  fi

  if ! inject_scenario "$scen"; then
    recover_scenario "$scen" || true
    wait_cluster_stable || true
    wait_baseline_zero || true
    record_status "$scen" "invalid" "$detectable" "not_run" "fault injection failed"
    continue
  fi

  if is_in_list "$scen" "${EXCLUDED_SCENARIOS[@]}"; then
    excluded_recovery_ok="true"
    recover_scenario "$scen" || excluded_recovery_ok="false"
    wait_cluster_stable || excluded_recovery_ok="false"
    wait_baseline_zero || excluded_recovery_ok="false"
    if [ "$excluded_recovery_ok" = "true" ]; then
      record_status "$scen" "excluded" "$detectable" "not_applicable" "excluded by EXCLUDED_SCENARIOS_CSV"
    else
      record_status "$scen" "invalid" "$detectable" "not_run" "excluded scenario recovery failed"
    fi
    continue
  fi

  log "  [대기] 5초..."; sleep 5

  detected="not_applicable"
  detection_api_ok="true"
  if [ "$detectable" = "false" ]; then
    log "  [스킵] Ground Truth detectable=false"
  else
    if wait_for_detection; then
      detected="true"
    else
      detection_rc=$?
      if [ "$detection_rc" -eq 1 ]; then
        detected="false"
      else
        detected="unknown"
        detection_api_ok="false"
      fi
    fi
  fi

  evaluation_ok="true"
  call_all_modes "$scen" || evaluation_ok="false"
  recovery_ok="true"
  recover_scenario "$scen" || recovery_ok="false"
  wait_cluster_stable || recovery_ok="false"

  if ! wait_baseline_zero; then
    recovery_ok="false"
  fi

  if [ "$recovery_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "recovery incomplete or dirty baseline after scenario"
    continue
  fi

  if [ "$detection_api_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "detection API produced no valid response"
    continue
  fi

  if [ "$evaluation_ok" != "true" ]; then
    record_status "$scen" "invalid" "$detectable" "$detected" "snapshot creation or mode evaluation failed"
    continue
  fi

  if [ "$detected" = "false" ]; then
    record_status "$scen" "valid" "$detectable" "$detected" "completed; valid false negative"
  else
    record_status "$scen" "valid" "$detectable" "$detected" "completed"
  fi
done

echo; echo "=========================================="; echo " 완료: $RESULT_DIR"; echo " invalid scenarios: $RUN_FAILURES"; echo "=========================================="
if [ "$RUN_FAILURES" -gt 0 ]; then
  exit 1
fi

if ! python3 - "$RESULT_DIR/_summary.tsv" "${SCENARIOS[@]}" <<'PY'
import csv
import sys

path, *expected = sys.argv[1:]
with open(path, newline="", encoding="utf-8") as stream:
    reader = csv.DictReader(stream, delimiter="\t")
    rows = list(reader)
required = ["scenario", "status", "detectable", "detected", "reason"]
if reader.fieldnames != required:
    raise SystemExit("invalid summary schema")
names = [row["scenario"] for row in rows]
if len(rows) != len(expected) or len(set(names)) != len(names) or set(names) != set(expected):
    raise SystemExit("incomplete or duplicate summary")
PY
then
  log "[실패] 최종 summary 무결성 검증 실패"
  exit 1
fi

pointer_tmp="$LATEST_POINTER.tmp.$$"
if ! printf '%s\n' "$RESULT_DIR" > "$pointer_tmp" || ! mv -f "$pointer_tmp" "$LATEST_POINTER"; then
  rm -f "$pointer_tmp"
  log "[실패] latest 결과 포인터 저장 실패"
  exit 1
fi
exit 0
