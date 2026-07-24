#!/usr/bin/env python3
import json, sys
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("judge_results_5cycle.json")

with open(path, encoding="utf-8") as f:
    data = json.load(f)

avgs = data["averages"]
modes = ["hybrid", "dfs_only", "llm_only"]

print("=" * 45)
print("   LLM-as-Judge 5사이클 평균 최종 결과")
print("=" * 45)
print(f"{'Judge':<10} {'hybrid':>8} {'dfs_only':>10} {'llm_only':>10}")
print("-" * 45)
for judge, m in avgs.items():
    values = ["N/A" if m[mode] is None else f"{m[mode]:.2f}" for mode in modes]
    print(f"{judge:<10} {values[0]:>8} {values[1]:>10} {values[2]:>10}")
print("-" * 45)
all_avg = {}
for mode in modes:
    values = [value[mode] for value in avgs.values() if value[mode] is not None]
    all_avg[mode] = sum(values) / len(values) if values else None
display = ["N/A" if all_avg[mode] is None else f"{all_avg[mode]:.2f}" for mode in modes]
print(f"{'평균':<10} {display[0]:>8} {display[1]:>10} {display[2]:>10}")
print("=" * 45)
