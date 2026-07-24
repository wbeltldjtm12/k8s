#!/usr/bin/env python3
# verify-results.py
import os
import sys
import json
from pathlib import Path

def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def load_trial_status(result_dir: str, scenario: str, payload: dict) -> str:
    status_path = Path(result_dir) / scenario / "status.txt"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status="):
                return line.split("=", 1)[1]
        return "invalid"
    return "valid" if payload else "invalid"

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
    if len(sys.argv) < 2:
        print("Usage: python3 eval/verify-results.py <results_dir>")
        sys.exit(1)

    result_dir = sys.argv[1]
    gt_path = Path(__file__).parent / "ground_truth.json"
    ground_truth = load_json(str(gt_path))
    
    print(f"{'No':<3} | {'Scenario':<22} | {'Target':<12} | {'Detect?':<8} | {'Hit@1':<6} | {'Root Cause':<25} | {'Reason'}")
    print("-" * 110)

    # 디렉터리 기반 검색
    scenarios = [d for d in os.listdir(result_dir) if os.path.isdir(os.path.join(result_dir, d))]
    
    # 레거시 파일 기반 fallback (이전 방식 단일 실행)
    if not scenarios:
        scenarios = [d.replace("_result.json", "") for d in os.listdir(result_dir) if d.endswith("_result.json")]

    scenarios.sort()
    
    hit_count = 0
    det_count = sum(1 for gt in ground_truth.values() if gt.get("detectable"))
    valid_detectable = 0
    excluded_count = 0
    invalid_count = 0

    for idx, scenario in enumerate(scenarios, 1):
        gt = ground_truth.get(scenario, {})
        target = gt.get("target", "?")
        detectable = gt.get("detectable", False)
        
        json_path = os.path.join(result_dir, scenario, "hybrid.json")
        if not os.path.exists(json_path):
            json_path = os.path.join(result_dir, f"{scenario}_result.json")
            
        data = load_json(json_path)
        trial_status = load_trial_status(result_dir, scenario, data)
        chains = data.get("data", {}).get("chains", []) if "data" in data else data.get("chains", [])
        quantitative = detectable and trial_status == "valid" and bool(data)
        if detectable:
            if quantitative:
                valid_detectable += 1
            elif trial_status == "excluded":
                excluded_count += 1
            else:
                invalid_count += 1
        
        rc_kind, rc_name, rc_reason = "", "", ""
        detect_str = "X"
        hit_str = "-"
        
        if trial_status == "excluded":
            detect_str = "EXCL"
            hit_str = "-"
        elif chains:
            detect_str = "O"
            c = chains[0]
            rc_kind = c.get("root_cause_kind", "")
            rc_name = c.get("root_cause", "")
            rc_reason = c.get("root_cause_reason", "Unavailable")
            if quantitative and is_hit(rc_kind, rc_name, gt):
                hit_str = "O"
                hit_count += 1
            else:
                hit_str = "X" if quantitative else ("N/A" if not detectable else "-")
        else:
            hit_str = "X" if quantitative else ("N/A" if not detectable else "-")
                
        rc_display = f"{rc_kind}/{rc_name}" if rc_kind else "-"
        print(f"{idx:<3} | {scenario:<22} | {target:<12} | {detect_str:<8} | {hit_str:<6} | {rc_display[:25]:<25} | {rc_reason[:20]}")

    print("-" * 110)
    invalid_count += max(0, det_count - valid_detectable - excluded_count - invalid_count)
    if valid_detectable:
        print(
            f"Total Hit@1 Accuracy: {hit_count}/{valid_detectable} = "
            f"{hit_count/valid_detectable*100:.1f}% (유효 detectable trial 기준)"
        )
    else:
        print("Total Hit@1 Accuracy: 유효 detectable trial 없음")
    print(
        f"Coverage: {valid_detectable}/{det_count}, "
        f"excluded={excluded_count}, invalid={invalid_count}"
    )

if __name__ == "__main__":
    main()
