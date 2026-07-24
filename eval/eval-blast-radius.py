#!/usr/bin/env python3
# eval-blast-radius.py
# KubeIn Blast Radius (파급 범위) 정확성 (Precision/Recall) 평가
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

def load_trial_status(result_dir: str, scenario: str, hybrid: dict) -> str:
    status_path = Path(result_dir) / scenario / "status.txt"
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status="):
                return line.split("=", 1)[1]
        return "invalid"
    return "valid" if hybrid.get("status") == "success" else "invalid"

def check_match(node_id: str, expected_list: list) -> bool:
    # node_id 예: "Pod/sock-shop/catalogue-db-86c68f475b-4rtwk"
    parts = node_id.split("/")
    if len(parts) < 3:
        return False
    kind = parts[0]
    name = parts[2]
    
    for exp in expected_list:
        if exp.get("kind") == kind and exp.get("name_contains", "") in name:
            return True
    return False

def check_gt_found(exp: dict, affected_ids: list) -> bool:
    for node_id in affected_ids:
        parts = node_id.split("/")
        if len(parts) >= 3:
            if exp.get("kind") == parts[0] and exp.get("name_contains", "") in parts[2]:
                return True
    return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 eval/eval-blast-radius.py <ablation_result_dir>")
        sys.exit(1)

    result_dir = sys.argv[1]
    gt_path = Path(__file__).parent / "ground_truth.json"
    ground_truth = load_json(str(gt_path))
    
    scenarios = sorted([d for d in os.listdir(result_dir) if os.path.isdir(os.path.join(result_dir, d))])
    
    print("=== Blast Radius 정확도 평가 (Hybrid 모드 기준) ===")
    print(f"{'Scenario':<22} | {'GT':<4} | {'Found':<5} | {'Match':<5} | {'Precision':<9} | {'Recall'}")
    print("-" * 65)

    tot_precision = []
    tot_recall = []
    nominal = sum(
        bool(value.get("detectable") and value.get("expected_affected"))
        for value in ground_truth.values()
    )
    valid_count = 0
    excluded_count = 0
    invalid_count = 0

    for scenario in scenarios:
        gt = ground_truth.get(scenario, {})
        detectable = gt.get("detectable", False)
        if not detectable:
            continue
            
        expected_affected = gt.get("expected_affected", [])
        if not expected_affected:
            continue
            
        hybrid_path = os.path.join(result_dir, scenario, "hybrid.json")
        hj = load_json(hybrid_path)
        trial_status = load_trial_status(result_dir, scenario, hj)
        if trial_status != "valid" or hj.get("status") != "success":
            if trial_status == "excluded":
                excluded_count += 1
            else:
                invalid_count += 1
            continue
        valid_count += 1
        chains = hj.get("data", {}).get("chains", [])
        
        affected_ids = sorted({
            node_id
            for chain in chains
            for node_id in chain.get("affected_node_ids", [])
            if isinstance(node_id, str)
        })
            
        # Precision = 매칭된 affected_ids 수 / 전체 affected_ids 수
        match_count = 0
        for node_id in affected_ids:
            if check_match(node_id, expected_affected):
                match_count += 1
                
        # Recall = 찾아낸 GT 예상 리소스 수 / 전체 GT 예상 리소스 수
        gt_found_count = 0
        for exp in expected_affected:
            if check_gt_found(exp, affected_ids):
                gt_found_count += 1
                
        gt_total = len(expected_affected)
        found_total = len(affected_ids)
        
        precision = (match_count / found_total * 100) if found_total > 0 else 0.0
        recall = (gt_found_count / gt_total * 100) if gt_total > 0 else 0.0
        
        tot_precision.append(precision)
        tot_recall.append(recall)
        
        print(f"{scenario:<22} | {gt_total:>4} | {found_total:>5} | {match_count:>5} | {precision:>8.1f}% | {recall:>5.1f}%")

    print("=" * 65)
    missing_count = max(0, nominal - valid_count - excluded_count - invalid_count)
    invalid_count += missing_count
    print(
        f"유효 trial coverage: {valid_count}/{nominal} "
        f"({valid_count / nominal * 100 if nominal else 0:.1f}%), "
        f"제외 {excluded_count} / invalid {invalid_count}"
    )
    if tot_precision and tot_recall:
        avg_p = sum(tot_precision) / len(tot_precision)
        avg_r = sum(tot_recall) / len(tot_recall)
        print(f"전체 평균: Precision={avg_p:.1f}%, Recall={avg_r:.1f}%")
    else:
        print("평가 데이터가 없습니다.")

if __name__ == "__main__":
    main()
