#!/usr/bin/env python3
# compare-5cycle.py
import os
import sys
import json
import statistics
from pathlib import Path

def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_trial_status(result_dir: str, scenario: str) -> str:
    """Canonical status를 읽고, 과거 결과는 세 mode JSON으로 보수적으로 추론합니다."""
    status_path = Path(result_dir) / scenario / "status.txt"
    if status_path.is_file():
        fields = {}
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        return fields.get("status", "invalid")

    payloads = [
        load_json(str(Path(result_dir) / scenario / f"{mode}.json"))
        for mode in ("hybrid", "dfs_only", "llm_only")
    ]
    return "valid" if all(p.get("status") == "success" for p in payloads) else "invalid"

def is_hit(rc_kind: str, rc_name: str, gt: dict) -> bool:
    if not gt.get("detectable"):
        return False
    exp_kind = gt.get("expected_rc_kind")
    exp_name = gt.get("expected_rc_name_contains")
    if exp_kind is None or exp_name is None or not isinstance(rc_kind, str) or not isinstance(rc_name, str):
        return False
    kinds = exp_kind if isinstance(exp_kind, list) else [exp_kind]
    names = exp_name if isinstance(exp_name, list) else [exp_name]
    return any(rc_kind == kind for kind in kinds) and any(
        isinstance(name, str) and name in rc_name for name in names
    )

def main():
    if len(sys.argv) != 6:
        print("Usage: python3 eval/compare-5cycle.py <dir1> <dir2> <dir3> <dir4> <dir5>")
        sys.exit(1)

    dirs = sys.argv[1:6]
    gt_path = Path(__file__).parent / "ground_truth.json"
    ground_truth = load_json(str(gt_path))
    
    detectable_scenarios = [s for s, gt in ground_truth.items() if gt.get("detectable")]
    dt = len(detectable_scenarios)
    if dt == 0:
        print("[ERROR] 탐지 가능 (detectable=true) 시나리오가 없습니다.")
        sys.exit(1)

    hybrid_accs = []
    dfs_accs = []
    llm_response_rates = []
    detection_rates = []
    coverage_rates = []
    invalid_total = 0
    excluded_total = 0
    
    h_times_all = []
    d_times_all = []
    l_times_all = []

    for d in dirs:
        h_hits = 0
        d_hits = 0
        llm_responses = 0
        detected_trials = 0
        eligible = 0
        
        for scenario in detectable_scenarios:
            gt = ground_truth.get(scenario, {})
            trial_status = load_trial_status(d, scenario)
            if trial_status != "valid":
                if trial_status == "excluded":
                    excluded_total += 1
                else:
                    invalid_total += 1
                continue

            hj = load_json(os.path.join(d, scenario, "hybrid.json"))
            dj = load_json(os.path.join(d, scenario, "dfs_only.json"))
            lj = load_json(os.path.join(d, scenario, "llm_only.json"))
            if not all(payload.get("status") == "success" for payload in (hj, dj, lj)):
                invalid_total += 1
                continue
            eligible += 1

            status_path = Path(d) / scenario / "status.txt"
            if status_path.is_file() and "detected=true" in status_path.read_text(encoding="utf-8").splitlines():
                detected_trials += 1
            elif not status_path.is_file() and dj.get("error_node_count", 0) > 0:
                detected_trials += 1

            hc = hj.get("data", {}).get("chains", [])
            if hc and is_hit(hc[0].get("root_cause_kind"), hc[0].get("root_cause"), gt):
                h_hits += 1
            
            dc = dj.get("data", {}).get("chains", [])
            if dc and is_hit(dc[0].get("root_cause_kind"), dc[0].get("root_cause"), gt):
                d_hits += 1

            ai_text = lj.get("data", {}).get("ai_analysis", "")
            if lj.get("status") == "success" and isinstance(ai_text, str) and ai_text.strip():
                llm_responses += 1

            ht = hj.get("timing_sec", 0.0)
            dts = dj.get("timing_sec", 0.0)
            lts = lj.get("timing_sec", 0.0)
            if ht > 0: h_times_all.append(ht)
            if dts > 0: d_times_all.append(dts)
            if lts > 0: l_times_all.append(lts)

        if eligible == 0:
            print(f"[ERROR] 유효한 detectable trial이 없습니다: {d}")
            sys.exit(1)
        hybrid_accs.append(h_hits / eligible * 100)
        dfs_accs.append(d_hits / eligible * 100)
        llm_response_rates.append(llm_responses / eligible * 100)
        detection_rates.append(detected_trials / eligible * 100)
        coverage_rates.append(eligible / dt * 100)

    # Calculate stats
    h_avg, h_std = statistics.mean(hybrid_accs), statistics.stdev(hybrid_accs) if len(hybrid_accs)>1 else 0
    d_avg, d_std = statistics.mean(dfs_accs), statistics.stdev(dfs_accs) if len(dfs_accs)>1 else 0
    l_avg = statistics.mean(llm_response_rates)
    l_std = statistics.stdev(llm_response_rates) if len(llm_response_rates) > 1 else 0
    det_avg = statistics.mean(detection_rates)
    det_std = statistics.stdev(detection_rates) if len(detection_rates) > 1 else 0
    cov_avg = statistics.mean(coverage_rates)
    cov_std = statistics.stdev(coverage_rates) if len(coverage_rates) > 1 else 0

    h_t_avg, h_t_std = statistics.mean(h_times_all) if h_times_all else 0, statistics.stdev(h_times_all) if len(h_times_all)>1 else 0
    d_t_avg, d_t_std = statistics.mean(d_times_all) if d_times_all else 0, statistics.stdev(d_times_all) if len(d_times_all)>1 else 0
    l_t_avg, l_t_std = statistics.mean(l_times_all) if l_times_all else 0, statistics.stdev(l_times_all) if len(l_times_all)>1 else 0

    print("=====================================================")
    print(" 5-Cycle Ablation Study 평균 ± 표준편차 결과")
    print("=====================================================")
    print("  RCA Hit@1 (detectable Ground Truth 기준)")
    print(f"    Hybrid:   {h_avg:.1f}% ± {h_std:.1f}%")
    print(f"    DFS-Only: {d_avg:.1f}% ± {d_std:.1f}%")
    print(f"  장애 탐지율: {det_avg:.1f}% ± {det_std:.1f}%")
    print(f"  유효 trial coverage: {cov_avg:.1f}% ± {cov_std:.1f}% (명목 {dt}건/cycle)")
    print(f"    제외 {excluded_total}건, 인프라/산출물 invalid {invalid_total}건 (전체 cycle 합계)")
    print("  LLM 응답 생성률 (내용 정확도가 아님)")
    print(f"    LLM-Only: {l_avg:.1f}% ± {l_std:.1f}%")
    print("-" * 53)
    print("  소요 시간 평균")
    print(f"    Hybrid:   {h_t_avg:.2f}s ± {h_t_std:.2f}s")
    print(f"    DFS-Only: {d_t_avg:.2f}s ± {d_t_std:.2f}s")
    print(f"    LLM-Only: {l_t_avg:.2f}s ± {l_t_std:.2f}s")
    print("=====================================================")

if __name__ == "__main__":
    main()
