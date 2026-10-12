#!/usr/bin/env bash
# 05-ablation-5cycle-cluster.sh
# 클러스터 환경용 5사이클 자동화 스크립트
#
# 사용법:
#   bash 05-ablation-5cycle-cluster.sh
#   CYCLES=3 bash 05-ablation-5cycle-cluster.sh    # 사이클 수 변경
#   SCENARIO_DIR=/custom/path/sock-shop-scenarios bash ...    # 경로 변경

set -uo pipefail

RUN_LOCK_FILE="${RUN_LOCK_FILE:-/tmp/kubein-ablation.lock}"
command -v flock >/dev/null 2>&1 || {
  echo "[오류] 동시 실행 방지에 필요한 flock을 찾을 수 없습니다." >&2
  exit 2
}
exec 9>"$RUN_LOCK_FILE"
if ! flock -n 9; then
  echo "[오류] 다른 KUBEIN ablation 실행이 이미 진행 중입니다: $RUN_LOCK_FILE" >&2
  exit 75
fi
KUBEIN_ABLATION_LOCK_HELD=1
export KUBEIN_ABLATION_LOCK_HELD RUN_LOCK_FILE

API_BASE="${API_BASE:-http://localhost:8000}"
CYCLES="${CYCLES:-5}"
RESULTS=()
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCENARIO_DIR="${SCENARIO_DIR:-$REPO_ROOT/cluster-port/sock-shop-scenarios}"
PRECHECK_SCRIPT="${PRECHECK_SCRIPT:-$SCRIPT_DIR/03-pre-check-cluster.sh}"
EVAL_SCRIPT="${EVAL_SCRIPT:-$SCRIPT_DIR/04-ablation-eval-cluster.sh}"
SCENARIO_MANIFEST="${SCENARIO_MANIFEST:-$REPO_ROOT/eval/scenarios.txt}"
INTEGRITY_SCRIPT="${INTEGRITY_SCRIPT:-$REPO_ROOT/eval/validate-ablation-results.py}"
SCORING_SCRIPT="${SCORING_SCRIPT:-$REPO_ROOT/eval/score-ablation-results.py}"
GROUND_TRUTH_FILE="${GROUND_TRUTH_FILE:-$REPO_ROOT/eval/ground_truth.json}"
EVAL_MODES_CSV="${EVAL_MODES_CSV:-hybrid,dfs_only,llm_only}"
EXCLUDED_SCENARIOS_CSV="${EXCLUDED_SCENARIOS_CSV-}"
LATEST_POINTER="$SCENARIO_DIR/results/latest_ablation_dir.txt"

export SCENARIO_DIR API_BASE SCENARIO_MANIFEST EVAL_MODES_CSV
export EXCLUDED_SCENARIOS_CSV GROUND_TRUTH_FILE

if ! [[ "$CYCLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "[오류] CYCLES는 1 이상의 정수여야 합니다: $CYCLES" >&2
  exit 2
fi
if [ ! -f "$PRECHECK_SCRIPT" ] || [ ! -f "$EVAL_SCRIPT" ] \
  || [ ! -f "$INTEGRITY_SCRIPT" ] || [ ! -f "$SCORING_SCRIPT" ]; then
  echo "[오류] runner 파일을 찾을 수 없습니다." >&2
  echo "  PRECHECK_SCRIPT=$PRECHECK_SCRIPT" >&2
  echo "  EVAL_SCRIPT=$EVAL_SCRIPT" >&2
  echo "  INTEGRITY_SCRIPT=$INTEGRITY_SCRIPT" >&2
  echo "  SCORING_SCRIPT=$SCORING_SCRIPT" >&2
  exit 2
fi
if [ ! -f "$SCENARIO_MANIFEST" ]; then
  echo "[오류] scenario manifest를 찾을 수 없습니다: $SCENARIO_MANIFEST" >&2
  exit 2
fi

run_precheck() {
  if bash "$PRECHECK_SCRIPT"; then
    return 0
  fi
  echo "[경고] pre-check 실패, 60초 후 1회 재시도"
  sleep 60
  bash "$PRECHECK_SCRIPT"
}

validate_three_mode_integrity() {
  local args=(
    "$INTEGRITY_SCRIPT"
    --manifest "$SCENARIO_MANIFEST"
    --modes hybrid,dfs_only,llm_only
    --require-valid
  )
  local cycle_dir
  for cycle_dir in "$@"; do
    args+=(--cycle-dir "$cycle_dir")
  done
  python3 "${args[@]}"
}

echo "================================================="
echo " Start ${CYCLES}-Cycle Ablation Study [클러스터]"
echo " SCENARIO_DIR: $SCENARIO_DIR"
echo " API_BASE    : $API_BASE"
echo "================================================="

echo "[1] 사전 점검"
if ! run_precheck; then
  echo "[오류] pre-check가 두 번 실패하여 실험을 중단합니다." >&2
  exit 1
fi

for i in $(seq 1 "$CYCLES"); do
  echo ""
  echo ">>> [CYCLE $i/$CYCLES] Start: $(date +'%Y-%m-%d %H:%M:%S')"

  if ! rm -f "$LATEST_POINTER"; then
    echo "[오류] stale latest 포인터를 제거할 수 없습니다: $LATEST_POINTER" >&2
    exit 1
  fi
  if ! bash "$EVAL_SCRIPT"; then
    echo "[오류] cycle $i 평가 실패. stale/부분 결과를 합치지 않고 중단합니다." >&2
    exit 1
  fi

  if [ ! -s "$LATEST_POINTER" ]; then
    echo "[오류] cycle $i latest 결과 포인터가 생성되지 않았습니다." >&2
    exit 1
  fi
  LATEST_DIR="$(<"$LATEST_POINTER")"
  case "$LATEST_DIR" in
    "$SCENARIO_DIR"/results/ablation_*) ;;
    *)
      echo "[오류] 예상 범위를 벗어난 결과 경로: $LATEST_DIR" >&2
      exit 1
      ;;
  esac
  if [ ! -f "$LATEST_DIR/_summary.tsv" ]; then
    echo "[오류] cycle $i summary가 없습니다: $LATEST_DIR/_summary.tsv" >&2
    exit 1
  fi
  if ! python3 - "$LATEST_DIR/_summary.tsv" "$SCENARIO_MANIFEST" <<'PY'
import csv
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as stream:
    reader = csv.DictReader(stream, delimiter="\t")
    rows = list(reader)
with open(sys.argv[2], encoding="utf-8") as stream:
    expected = [
        line.strip()
        for line in stream
        if line.strip() and not line.lstrip().startswith("#")
    ]
required = ["scenario", "status", "detectable", "detected", "reason"]
names = [row.get("scenario", "") for row in rows]
if (
    reader.fieldnames != required
    or len(expected) != 20
    or len(set(expected)) != 20
    or names != expected
):
    raise SystemExit(1)
PY
  then
    echo "[오류] cycle $i summary 스키마/20개 scenario 검증 실패" >&2
    exit 1
  fi
  if ! validate_three_mode_integrity "$LATEST_DIR"; then
    echo "[오류] cycle $i의 20시나리오/3모드 결과 무결성 검증 실패" >&2
    exit 1
  fi
  RESULTS+=("$LATEST_DIR")

  echo ">>> [CYCLE $i/$CYCLES] End: $(date +'%Y-%m-%d %H:%M:%S')"
  echo ">>> Result: $LATEST_DIR"

  if [ "$i" -lt "$CYCLES" ]; then
    echo ">>> 다음 cycle 사전 점검"
    if ! run_precheck; then
      echo "[오류] pre-check가 두 번 실패하여 다음 cycle을 중단합니다." >&2
      exit 1
    fi
    echo ">>> 클러스터 안정화 대기 60초"
    sleep 60
  fi
done

echo ""
echo "================================================="
echo " ${CYCLES}-Cycle 완료!"
echo "================================================="
echo "결과 디렉터리:"
for r in "${RESULTS[@]}"; do echo " - $r"; done

# ── 통합 집계 ──────────────────────────────────────────
MERGE_DIR="$(mktemp -d "$SCENARIO_DIR/results/ablation_${CYCLES}cycle_merged_$(date +%Y%m%d_%H%M%S)_XXXXXX")" || exit 1
printf 'cycle\tscenario\tstatus\tdetectable\tdetected\treason\n' > "$MERGE_DIR/_merged_summary.tsv" || exit 1
printf '%s\n' "${RESULTS[@]}" > "$MERGE_DIR/_cycle_dirs.txt" || exit 1

cycle_num=1
for r in "${RESULTS[@]}"; do
  if [ -f "$r/_summary.tsv" ]; then
    tail -n +2 "$r/_summary.tsv" | while IFS=$'\t' read -r scen status detectable detected reason; do
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$cycle_num" "$scen" "$status" "$detectable" "$detected" "$reason"
    done >> "$MERGE_DIR/_merged_summary.tsv" || exit 1
  fi
  cycle_num=$((cycle_num+1))
done

if [ "$(printf '%s\n' "${RESULTS[@]}" | sort -u | wc -l | tr -d ' ')" -ne "$CYCLES" ]; then
  echo "[오류] cycle 결과 디렉터리가 중복되었습니다." >&2
  exit 1
fi

if ! validate_three_mode_integrity "${RESULTS[@]}"; then
  echo "[오류] 전체 cycle 20시나리오/3모드 통합 무결성 검증 실패" >&2
  exit 1
fi

score_args=(
  "$SCORING_SCRIPT"
  --ground-truth "$GROUND_TRUTH_FILE"
  --manifest "$SCENARIO_MANIFEST"
  --output-json "$MERGE_DIR/scoring.json"
  --output-tsv "$MERGE_DIR/scoring.tsv"
)
for r in "${RESULTS[@]}"; do
  score_args+=(--cycle-dir "$r")
done
if ! python3 "${score_args[@]}"; then
  echo "[오류] 결정론적 batch scoring 실패" >&2
  exit 1
fi

echo ""
echo "통합 결과: $MERGE_DIR/_merged_summary.tsv"
echo "무결성/채점: $MERGE_DIR/scoring.json"
echo "무결성/채점 TSV: $MERGE_DIR/scoring.tsv"
