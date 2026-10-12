#!/usr/bin/env python3
"""Deterministically score validated KUBEIN ablation results.

Hybrid and DFS-only structured root-cause chains are scored against index-wise
Ground Truth tuples. LLM-only is deliberately limited to response-integrity
reporting because its current response is free text rather than a structured
root-cause prediction.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Callable, Iterable


CONTENT_MODES = ("hybrid", "dfs_only")
ALL_MODES = ("hybrid", "dfs_only", "llm_only")


class ScoringError(ValueError):
    """Raised when inputs are invalid or cannot be scored."""


def _load_validator_module():
    path = Path(__file__).with_name("validate-ablation-results.py")
    spec = importlib.util.spec_from_file_location(
        "kubein_validate_ablation_results", path
    )
    if spec is None or spec.loader is None:
        raise ScoringError(f"cannot load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator_module()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringError(f"cannot read validated response {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScoringError(f"response must be an object: {path}")
    return payload


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.mean(materialized) if materialized else 0.0


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _prediction_tuple(chain: dict[str, Any]) -> tuple[str, str, str]:
    return (
        chain["root_cause_kind"],
        chain["root_cause"],
        chain["root_cause_reason"],
    )


def tuple_matches(
    expected: tuple[str, str, str],
    predicted: tuple[str, str, str],
    *,
    include_reason: bool = True,
) -> bool:
    """Match one complete tuple; the Ground Truth name is a substring pattern."""

    expected_kind, expected_name, expected_reason = expected
    predicted_kind, predicted_name, predicted_reason = predicted
    return (
        predicted_kind == expected_kind
        and expected_name in predicted_name
        and (not include_reason or predicted_reason == expected_reason)
    )


def maximum_bipartite_matching(
    expected: list[tuple[str, str, str]],
    predicted: list[tuple[str, str, str]],
    *,
    include_reason: bool = True,
) -> list[tuple[int, int]]:
    """Return deterministic maximum-cardinality expected/predicted index pairs."""

    adjacency = [
        [
            predicted_index
            for predicted_index, predicted_tuple in enumerate(predicted)
            if tuple_matches(
                expected_tuple,
                predicted_tuple,
                include_reason=include_reason,
            )
        ]
        for expected_tuple in expected
    ]
    matched_prediction: dict[int, int] = {}

    def augment(expected_index: int, visited: set[int]) -> bool:
        for predicted_index in adjacency[expected_index]:
            if predicted_index in visited:
                continue
            visited.add(predicted_index)
            previous_expected = matched_prediction.get(predicted_index)
            if previous_expected is None or augment(previous_expected, visited):
                matched_prediction[predicted_index] = expected_index
                return True
        return False

    for expected_index in range(len(expected)):
        augment(expected_index, set())
    return sorted(
        (expected_index, predicted_index)
        for predicted_index, expected_index in matched_prediction.items()
    )


def _score_prediction_set(
    expected: list[tuple[str, str, str]],
    predicted: list[tuple[str, str, str]],
    top_k: int,
) -> dict[str, Any]:
    selected = predicted[:top_k]
    strict_pairs = maximum_bipartite_matching(expected, selected)
    identity_pairs = maximum_bipartite_matching(
        expected, selected, include_reason=False
    )
    top_one_pairs = maximum_bipartite_matching(expected, selected[:1])
    matched = len(strict_pairs)
    precision = _ratio(matched, len(selected))
    recall = _ratio(matched, len(expected))
    f1 = _ratio(2.0 * precision * recall, precision + recall)
    return {
        "expected_count": len(expected),
        "predicted_count": len(selected),
        "matched_expected": matched,
        "matched_pairs": [
            {
                "expected_index": expected_index,
                "predicted_index": predicted_index,
            }
            for expected_index, predicted_index in strict_pairs
        ],
        "hit_at_1": bool(top_one_pairs),
        "hit_at_k": matched > 0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exact_set": (
            matched == len(expected)
            and matched == len(selected)
            and len(expected) > 0
        ),
        "identity_match_count_without_reason": len(identity_pairs),
        "reason_mismatch_count": max(0, len(identity_pairs) - matched),
    }


def _confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(row["detectable"] and row["predicted_positive"] for row in rows)
    fn = sum(row["detectable"] and not row["predicted_positive"] for row in rows)
    fp = sum(not row["detectable"] and row["predicted_positive"] for row in rows)
    tn = sum(
        not row["detectable"] and not row["predicted_positive"] for row in rows
    )
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    specificity = _ratio(tn, tn + fp)
    f1 = _ratio(2.0 * precision * recall, precision + recall)
    return {
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "f1": f1,
        "accuracy": _ratio(tp + tn, len(rows)),
    }


def _timing_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["timing_sec"]) for row in rows]
    return {
        "n": len(values),
        "mean_sec": _mean(values),
        "median_sec": statistics.median(values) if values else 0.0,
        "p95_sec": _percentile(values, 0.95),
        "min_sec": min(values) if values else 0.0,
        "max_sec": max(values) if values else 0.0,
    }


def _aggregate_content_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    detectable_rows = [row for row in rows if row["detectable"]]
    return {
        "trial_count": len(rows),
        "detectable_trial_count": len(detectable_rows),
        "hit_at_1_count": sum(
            bool(row["hit_at_1"]) for row in detectable_rows
        ),
        "hit_at_1_rate": _mean(
            float(bool(row["hit_at_1"])) for row in detectable_rows
        ),
        "hit_at_k_count": sum(
            bool(row["hit_at_k"]) for row in detectable_rows
        ),
        "hit_at_k_rate": _mean(
            float(bool(row["hit_at_k"])) for row in detectable_rows
        ),
        "exact_set_count": sum(
            bool(row["exact_set"]) for row in detectable_rows
        ),
        "exact_set_rate": _mean(
            float(bool(row["exact_set"])) for row in detectable_rows
        ),
        "macro_precision": _mean(
            float(row["precision"]) for row in detectable_rows
        ),
        "macro_recall": _mean(float(row["recall"]) for row in detectable_rows),
        "macro_f1": _mean(float(row["f1"]) for row in detectable_rows),
        "reason_mismatch_count": sum(
            int(row["reason_mismatch_count"]) for row in detectable_rows
        ),
        "detection_confusion": _confusion(rows),
        "timing": _timing_summary(rows),
    }


def _aggregate_llm_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "policy": "integrity_only",
        "content_accuracy": None,
        "reason": (
            "llm_only currently returns free-text ai_analysis and no structured "
            "root-cause tuples; deterministic Ground Truth content scoring is "
            "therefore not claimed"
        ),
        "trial_count": len(rows),
        "integrity_pass_count": sum(row["response_integrity"] for row in rows),
        "nonempty_ai_text_count": sum(
            row["ai_text_length"] > 0 for row in rows
        ),
        "ai_success_count": sum(row["ai_status"] == "success" for row in rows),
        "ai_not_applicable_count": sum(
            row["ai_status"] == "not_applicable" for row in rows
        ),
        "timing": _timing_summary(rows),
    }


def _coverage_summary(
    nominal_scenarios: int,
    evaluated_scenarios: int,
    excluded_scenarios: int,
) -> dict[str, Any]:
    nominal_modes = nominal_scenarios * len(ALL_MODES)
    evaluated_modes = evaluated_scenarios * len(ALL_MODES)
    excluded_modes = excluded_scenarios * len(ALL_MODES)
    return {
        "nominal_scenario_count": nominal_scenarios,
        "evaluated_scenario_count": evaluated_scenarios,
        "excluded_scenario_count": excluded_scenarios,
        "scenario_coverage_rate": _ratio(
            evaluated_scenarios, nominal_scenarios
        ),
        "nominal_mode_count": nominal_modes,
        "evaluated_mode_response_count": evaluated_modes,
        "excluded_mode_count": excluded_modes,
    }


def score_cycles(
    cycle_dirs: Iterable[Path | str],
    manifest_path: Path | str,
    ground_truth_path: Path | str,
    *,
    top_k: int = 5,
    require_valid: bool = False,
) -> dict[str, Any]:
    if top_k < 1:
        raise ScoringError("top_k must be at least 1")
    resolved_cycles = [Path(path).expanduser().resolve() for path in cycle_dirs]
    try:
        validation = VALIDATOR.validate_cycles(
            resolved_cycles,
            manifest_path,
            ground_truth_path,
            require_valid=require_valid,
        )
    except VALIDATOR.InputError as exc:
        raise ScoringError(str(exc)) from exc
    if not validation["valid"]:
        messages = list(validation["global_errors"])
        for cycle in validation["cycles"]:
            messages.extend(cycle["errors"])
            for scenario in cycle["scenarios"]:
                messages.extend(
                    f"{scenario['scenario']}: {error}"
                    for error in scenario["errors"]
                )
        preview = "; ".join(messages[:10])
        if len(messages) > 10:
            preview += f"; ... ({len(messages) - 10} more)"
        raise ScoringError(f"result integrity validation failed: {preview}")

    manifest = Path(manifest_path).expanduser().resolve()
    ground_truth_file = Path(ground_truth_path).expanduser().resolve()
    scenarios = VALIDATOR.load_manifest(manifest)
    ground_truth = VALIDATOR.load_ground_truth(ground_truth_file, scenarios)

    detail_rows: list[dict[str, Any]] = []
    for cycle_index, cycle_dir in enumerate(resolved_cycles, 1):
        validation_scenarios = {
            item["scenario"]: item
            for item in validation["cycles"][cycle_index - 1]["scenarios"]
        }
        for scenario in scenarios:
            gt_entry = ground_truth[scenario]
            expected = (
                VALIDATOR.expected_root_cause_tuples(gt_entry)
                if gt_entry["detectable"]
                else []
            )
            scenario_status = validation_scenarios[scenario]["status"]
            for mode in ALL_MODES:
                if scenario_status == "excluded":
                    base: dict[str, Any] = {
                        "cycle_index": cycle_index,
                        "cycle_dir": str(cycle_dir),
                        "scenario": scenario,
                        "mode": mode,
                        "status": "excluded",
                        "evaluation_status": "not_run",
                        "detectable": gt_entry["detectable"],
                        "response_integrity": None,
                        "timing_sec": None,
                        "error_node_count": None,
                        "scoring_policy": "not_run",
                        "predicted_positive": None,
                        "note": VALIDATOR.EXCLUDED_REASON,
                    }
                    if mode == "llm_only":
                        base.update(
                            {
                                "ai_status": "not_run",
                                "ai_text_length": None,
                            }
                        )
                    else:
                        base.update(
                            {
                                "expected_count": None,
                                "predicted_count": None,
                                "matched_expected": None,
                                "matched_pairs": [],
                                "hit_at_1": None,
                                "hit_at_k": None,
                                "precision": None,
                                "recall": None,
                                "f1": None,
                                "exact_set": None,
                                "identity_match_count_without_reason": None,
                                "reason_mismatch_count": None,
                            }
                        )
                    detail_rows.append(base)
                    continue

                payload = _load_json(cycle_dir / scenario / f"{mode}.json")
                data = payload["data"]
                base = {
                    "cycle_index": cycle_index,
                    "cycle_dir": str(cycle_dir),
                    "scenario": scenario,
                    "mode": mode,
                    "status": "valid",
                    "evaluation_status": "evaluated",
                    "detectable": gt_entry["detectable"],
                    "response_integrity": True,
                    "timing_sec": float(payload["timing_sec"]),
                    "error_node_count": payload["error_node_count"],
                }
                if mode == "llm_only":
                    base.update(
                        {
                            "scoring_policy": "integrity_only",
                            "ai_status": data["ai_status"],
                            "ai_text_length": len(data["ai_analysis"]),
                            "predicted_positive": None,
                            "note": (
                                "free-text response; deterministic content "
                                "accuracy is not scored"
                            ),
                        }
                    )
                else:
                    predicted = [
                        _prediction_tuple(chain) for chain in data["chains"]
                    ]
                    metrics = (
                        _score_prediction_set(expected, predicted, top_k)
                        if gt_entry["detectable"]
                        else {
                            "expected_count": 0,
                            "predicted_count": len(predicted[:top_k]),
                            "matched_expected": 0,
                            "matched_pairs": [],
                            "hit_at_1": None,
                            "hit_at_k": None,
                            "precision": None,
                            "recall": None,
                            "f1": None,
                            "exact_set": None,
                            "identity_match_count_without_reason": 0,
                            "reason_mismatch_count": 0,
                        }
                    )
                    base.update(metrics)
                    base.update(
                        {
                            "scoring_policy": "structured_gt_tuple",
                            "predicted_positive": bool(predicted),
                            "note": "",
                        }
                    )
                detail_rows.append(base)

    cycle_aggregates: list[dict[str, Any]] = []
    for cycle_index in range(1, len(resolved_cycles) + 1):
        validation_cycle = validation["cycles"][cycle_index - 1]
        per_cycle: dict[str, Any] = {
            "cycle_index": cycle_index,
            "coverage": _coverage_summary(
                20,
                validation_cycle["evaluated_scenario_count"],
                validation_cycle["excluded_scenario_count"],
            ),
            "modes": {},
        }
        for mode in CONTENT_MODES:
            rows = [
                row
                for row in detail_rows
                if row["cycle_index"] == cycle_index
                and row["mode"] == mode
                and row["evaluation_status"] == "evaluated"
            ]
            per_cycle["modes"][mode] = _aggregate_content_rows(rows)
        llm_rows = [
            row
            for row in detail_rows
            if row["cycle_index"] == cycle_index
            and row["mode"] == "llm_only"
            and row["evaluation_status"] == "evaluated"
        ]
        per_cycle["modes"]["llm_only"] = _aggregate_llm_rows(llm_rows)
        cycle_aggregates.append(per_cycle)

    overall: dict[str, Any] = {}
    for mode in CONTENT_MODES:
        rows = [
            row
            for row in detail_rows
            if row["mode"] == mode
            and row["evaluation_status"] == "evaluated"
        ]
        aggregate = _aggregate_content_rows(rows)
        cycle_hit_rates = [
            cycle["modes"][mode]["hit_at_1_rate"] for cycle in cycle_aggregates
        ]
        aggregate["cycle_hit_at_1_rate_mean"] = _mean(cycle_hit_rates)
        aggregate["cycle_hit_at_1_rate_sample_stdev"] = (
            statistics.stdev(cycle_hit_rates)
            if len(cycle_hit_rates) > 1
            else 0.0
        )
        overall[mode] = aggregate
    overall["llm_only"] = _aggregate_llm_rows(
        [
            row
            for row in detail_rows
            if row["mode"] == "llm_only"
            and row["evaluation_status"] == "evaluated"
        ]
    )

    evaluated_scenarios = validation["totals"]["evaluated_scenario_records"]
    excluded_scenarios = validation["totals"]["excluded_scenario_records"]
    nominal_scenarios = len(resolved_cycles) * len(scenarios)
    coverage = _coverage_summary(
        nominal_scenarios, evaluated_scenarios, excluded_scenarios
    )

    return {
        "schema_version": "1.0",
        "valid": True,
        "manifest": str(manifest),
        "ground_truth": str(ground_truth_file),
        "cycle_dirs": [str(path) for path in resolved_cycles],
        "scoring_policy": {
            "content_modes": list(CONTENT_MODES),
            "llm_only": "integrity_only",
            "excluded": "not_run; excluded from all metric and timing denominators",
            "top_k": top_k,
            "tuple_alignment": (
                "Ground Truth arrays are paired index-wise as "
                "(kind, name_contains, reason)"
            ),
            "tuple_match": (
                "kind exact AND expected name substring in predicted name "
                "AND reason exact"
            ),
            "matching": "maximum-cardinality bipartite matching",
        },
        "totals": {
            "cycle_count": len(resolved_cycles),
            "scenario_count": nominal_scenarios,
            "mode_response_count": coverage["evaluated_mode_response_count"],
            "detail_row_count": len(detail_rows),
            "detectable_scenarios_per_cycle": sum(
                entry["detectable"] for entry in ground_truth.values()
            ),
        },
        "coverage": coverage,
        "cycle_aggregates": cycle_aggregates,
        "overall": overall,
        "details": detail_rows,
    }


def write_json_report(report: dict[str, Any], path: Path | str) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_tsv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return value


def write_tsv_report(report: dict[str, Any], path: Path | str) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "row_type",
        "cycle_index",
        "cycle_dir",
        "scenario",
        "mode",
        "status",
        "evaluation_status",
        "detectable",
        "response_integrity",
        "expected_count",
        "predicted_count",
        "matched_expected",
        "hit_at_1",
        "hit_at_k",
        "precision",
        "recall",
        "f1",
        "exact_set",
        "predicted_positive",
        "timing_sec",
        "error_node_count",
        "ai_status",
        "ai_text_length",
        "note",
        "trial_count",
        "hit_at_1_rate",
        "hit_at_k_rate",
        "exact_set_rate",
        "tp",
        "tn",
        "fp",
        "fn",
        "specificity",
        "accuracy",
        "timing_mean_sec",
        "timing_median_sec",
        "timing_p95_sec",
        "nominal_scenario_count",
        "evaluated_scenario_count",
        "excluded_scenario_count",
        "scenario_coverage_rate",
        "nominal_mode_count",
        "evaluated_mode_response_count",
        "excluded_mode_count",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fields,
            delimiter="\t",
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in report["details"]:
            payload = {"row_type": "scenario", **row}
            writer.writerow(
                {key: _format_tsv_value(value) for key, value in payload.items()}
            )
        writer.writerow(
            {
                key: _format_tsv_value(value)
                for key, value in {
                    "row_type": "coverage",
                    **report["coverage"],
                }.items()
            }
        )
        for mode in ALL_MODES:
            aggregate = report["overall"][mode]
            payload: dict[str, Any] = {
                "row_type": "aggregate",
                "cycle_index": "all",
                "mode": mode,
                "trial_count": aggregate["trial_count"],
                "note": aggregate.get("reason", ""),
            }
            timing = aggregate["timing"]
            payload.update(
                {
                    "timing_mean_sec": timing["mean_sec"],
                    "timing_median_sec": timing["median_sec"],
                    "timing_p95_sec": timing["p95_sec"],
                }
            )
            if mode in CONTENT_MODES:
                confusion = aggregate["detection_confusion"]
                payload.update(
                    {
                        "hit_at_1_rate": aggregate["hit_at_1_rate"],
                        "hit_at_k_rate": aggregate["hit_at_k_rate"],
                        "exact_set_rate": aggregate["exact_set_rate"],
                        "precision": aggregate["macro_precision"],
                        "recall": aggregate["macro_recall"],
                        "f1": aggregate["macro_f1"],
                        "tp": confusion["tp"],
                        "tn": confusion["tn"],
                        "fp": confusion["fp"],
                        "fn": confusion["fn"],
                        "specificity": confusion["specificity"],
                        "accuracy": confusion["accuracy"],
                    }
                )
            writer.writerow(
                {key: _format_tsv_value(value) for key, value in payload.items()}
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and deterministically score one or more KUBEIN ablation "
            "cycle directories."
        )
    )
    parser.add_argument(
        "--ground-truth", required=True, type=Path, help="Ground Truth JSON"
    )
    parser.add_argument(
        "--manifest", required=True, type=Path, help="20-scenario manifest"
    )
    parser.add_argument(
        "--cycle-dir",
        dest="cycle_dirs",
        action="append",
        required=True,
        type=Path,
        help="Cycle result directory; repeat for multiple cycles.",
    )
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-tsv", required=True, type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--require-valid",
        action="store_true",
        help="Reject excluded scenarios and require all nominal trials to run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = score_cycles(
            args.cycle_dirs,
            args.manifest,
            args.ground_truth,
            top_k=args.top_k,
            require_valid=args.require_valid,
        )
    except ScoringError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    write_json_report(report, args.output_json)
    write_tsv_report(report, args.output_tsv)
    hybrid = report["overall"]["hybrid"]
    dfs = report["overall"]["dfs_only"]
    print(
        "[PASS] "
        f"cycles={report['totals']['cycle_count']} "
        f"responses={report['totals']['mode_response_count']} "
        f"coverage={report['coverage']['evaluated_scenario_count']}/"
        f"{report['coverage']['nominal_scenario_count']} "
        f"Hybrid Hit@1={hybrid['hit_at_1_rate']:.4f} "
        f"DFS Hit@1={dfs['hit_at_1_rate']:.4f}; "
        "LLM-only=integrity-only"
    )
    print(f"JSON: {args.output_json}")
    print(f"TSV:  {args.output_tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
