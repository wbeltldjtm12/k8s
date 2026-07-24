#!/usr/bin/env python3
# compare-results.py
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

def load_trial_status(result_dir: str, scenario: str) -> str:
    status_path = Path(result_dir) / scenario / "status.txt"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status="):
                return line.split("=", 1)[1]
        return "invalid"
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
    if len(sys.argv) < 2:
        print("Usage: python3 eval/compare-results.py <ablation_result_dir>")
        sys.exit(1)

    result_dir = sys.argv[1]
    gt_path = Path(__file__).parent / "ground_truth.json"
    ground_truth = load_json(str(gt_path))
    
    if not ground_truth:
        print("[ERROR] eval/ground_truth.json 을 찾을 수 없습니다.")
        sys.exit(1)

    scenarios = sorted([d for d in os.listdir(result_dir) if os.path.isdir(os.path.join(result_dir, d))])
    
    print(f"\n{'='*140}")
    print(f"{'Scenario':<22} | {'GT Target':<14} | {'[Hybrid] RC Kind/Name/Reason':<33} | {'[DFS] RC Kind/Name/Reason':<33} | {'LLM Resp':<8} | {'H-Sec':<6} | {'D-Sec':<6}")
    print(f"{'-'*140}")

    stats = {
        "hybrid_hits": 0, "dfs_hits": 0, "llm_responses": 0,
        "detectable_nominal": sum(bool(value.get("detectable")) for value in ground_truth.values()),
        "detectable_valid": 0,
        "excluded": 0, "invalid": 0,
    }
    seen_detectable = set()

    for scenario in scenarios:
        gt = ground_truth.get(scenario, {})
        target = gt.get("target", "?")
        detectable = gt.get("detectable", False)
        
        if detectable:
            seen_detectable.add(scenario)

        trial_status = load_trial_status(result_dir, scenario)

        hj = load_json(os.path.join(result_dir, scenario, "hybrid.json"))
        dj = load_json(os.path.join(result_dir, scenario, "dfs_only.json"))
        lj = load_json(os.path.join(result_dir, scenario, "llm_only.json"))

        hc = hj.get("data", {}).get("chains", [])
        dc = dj.get("data", {}).get("chains", [])
        
        h_rc_kind, h_rc_name, h_rc_reason = "-", "-", "-"
        if hc:
            h_rc_kind = hc[0].get("root_cause_kind", "-")
            h_rc_name = hc[0].get("root_cause", "-")
            h_rc_reason = hc[0].get("root_cause_reason", "-")
            
        d_rc_kind, d_rc_name, d_rc_reason = "-", "-", "-"
        if dc:
            d_rc_kind = dc[0].get("root_cause_kind", "-")
            d_rc_name = dc[0].get("root_cause", "-")
            d_rc_reason = dc[0].get("root_cause_reason", "-")

        ai_an = lj.get("data", {}).get("ai_analysis", "")
        llm_responded = (
            lj.get("status") == "success"
            and isinstance(ai_an, str)
            and bool(ai_an.strip())
        )
        llm_response_mark = "O" if llm_responded else "X"

        h_time = hj.get("timing_sec", 0.0)
        d_time = dj.get("timing_sec", 0.0)

        # Hit@1 판정 (detectable=False일 경우 모두 Hit 판정 X)
        quantitative = detectable and trial_status == "valid" and all(
            payload.get("status") == "success" for payload in (hj, dj, lj)
        )
        if detectable and not quantitative:
            if trial_status == "excluded":
                stats["excluded"] += 1
            else:
                stats["invalid"] += 1
        if quantitative:
            stats["detectable_valid"] += 1

        if quantitative and is_hit(h_rc_kind, h_rc_name, gt):
            stats["hybrid_hits"] += 1
        if quantitative and is_hit(d_rc_kind, d_rc_name, gt):
            stats["dfs_hits"] += 1
        if quantitative and llm_responded:
            stats["llm_responses"] += 1

        h_str = f"{h_rc_kind}/{h_rc_name}/{h_rc_reason}"
        d_str = f"{d_rc_kind}/{d_rc_name}/{d_rc_reason}"
        print(f"{scenario:<22} | {target:<14} | {h_str[:33]:<33} | {d_str[:33]:<33} | {llm_response_mark:<8} | {h_time:<6.2f} | {d_time:<6.2f}")

    print(f"{'='*140}")
    stats["invalid"] += stats["detectable_nominal"] - len(seen_detectable)
    
    dt = stats["detectable_valid"]
    if dt > 0:
        h_acc = stats["hybrid_hits"] / dt * 100
        d_acc = stats["dfs_hits"] / dt * 100
        llm_response_rate = stats["llm_responses"] / dt * 100
        print(f"\n[정확도 요약] (탐지가능 {dt}개 시나리오 기준 Hit@1 비율)")
        print(f" - Hybrid:   {h_acc:.1f}% ({stats['hybrid_hits']}/{dt})")
        print(f" - DFS-Only: {d_acc:.1f}% ({stats['dfs_hits']}/{dt})")
        print("\n[응답 생성률] (내용 정확도가 아님)")
        print(f" - LLM-Only: {llm_response_rate:.1f}% (비어 있지 않은 정상응답 {stats['llm_responses']}/{dt})")
        nominal = stats["detectable_nominal"]
        print(
            f"\n[유효 trial coverage] {dt}/{nominal} ({dt / nominal * 100:.1f}%), "
            f"제외 {stats['excluded']} / invalid {stats['invalid']}"
        )
        print()
    else:
        print("\n[ERROR] 유효한 detectable trial이 없습니다.")
        sys.exit(1)

if __name__ == "__main__":
    main()
