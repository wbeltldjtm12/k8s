#!/usr/bin/env python3
"""Validate KUBEIN ablation result directories without external dependencies.

Every cycle must contain exactly the 20 scenarios from the manifest. Executed
(``valid``) trials must contain all three modes for one evaluation snapshot.
Explicitly ``excluded`` trials are accepted by default as not-run records, but
the canonical ``--require-valid`` gate still requires all 20 trials to run.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable


REQUIRED_MODES = ("hybrid", "dfs_only", "llm_only")
SUMMARY_FIELDS = ("scenario", "status", "detectable", "detected", "reason")
STATUS_FIELDS = SUMMARY_FIELDS
SNAPSHOT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
DETECTED_VALUES = {"true", "false", "unknown", "not_run", "not_applicable"}
AI_STATUSES = {"success", "not_applicable"}
SCENARIO_STATUSES = {"valid", "excluded"}
EXCLUDED_REASON = "excluded by EXCLUDED_SCENARIOS_CSV"


class InputError(ValueError):
    """Raised when a manifest or Ground Truth file cannot define an evaluation."""


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_manifest(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InputError(f"cannot read scenario manifest {path}: {exc}") from exc
    scenarios = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(scenarios) != 20:
        raise InputError(
            f"scenario manifest must contain exactly 20 entries, got {len(scenarios)}"
        )
    if len(set(scenarios)) != len(scenarios):
        raise InputError("scenario manifest contains duplicate entries")
    return scenarios


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def expected_root_cause_tuples(entry: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return index-wise (kind, name substring, reason) Ground Truth tuples."""

    kinds = _as_list(entry.get("expected_rc_kind"))
    names = _as_list(entry.get("expected_rc_name_contains"))
    reasons = _as_list(entry.get("expected_rc_reason"))
    lengths = {len(kinds), len(names), len(reasons)}
    if lengths != {len(kinds)}:
        raise InputError(
            "expected_rc_kind, expected_rc_name_contains, and expected_rc_reason "
            "must have equal lengths"
        )
    if not kinds or any(
        not isinstance(value, str) or not value
        for values in (kinds, names, reasons)
        for value in values
    ):
        raise InputError("expected root-cause tuple values must be non-empty strings")
    return list(zip(kinds, names, reasons))


def load_ground_truth(path: Path, scenarios: Iterable[str]) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read Ground Truth {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError("Ground Truth top level must be an object")

    active: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        entry = payload.get(scenario)
        if not isinstance(entry, dict):
            raise InputError(f"Ground Truth entry is missing or invalid: {scenario}")
        if not isinstance(entry.get("detectable"), bool):
            raise InputError(
                f"Ground Truth detectable must be boolean: {scenario}"
            )
        if entry["detectable"]:
            try:
                expected_root_cause_tuples(entry)
            except InputError as exc:
                raise InputError(f"{scenario}: {exc}") from exc
        affected = entry.get("expected_affected", [])
        if not isinstance(affected, list):
            raise InputError(f"expected_affected must be a list: {scenario}")
        active[scenario] = entry
    return active


def _read_json_object(path: Path, errors: list[str], label: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing {label}: {path.name}")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid {label} {path.name}: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"{label} must be a JSON object: {path.name}")
        return None
    return payload


def _read_status(path: Path, errors: list[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        errors.append("missing status.txt")
        return {}
    except OSError as exc:
        errors.append(f"cannot read status.txt: {exc}")
        return {}

    result: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if "=" not in line:
            errors.append(f"status.txt line {line_number} is not key=value")
            continue
        key, value = line.split("=", 1)
        if key in result:
            errors.append(f"status.txt has duplicate key: {key}")
            continue
        result[key] = value
    missing = [field for field in STATUS_FIELDS if field not in result]
    extra = sorted(set(result) - set(STATUS_FIELDS))
    if missing:
        errors.append(f"status.txt missing fields: {','.join(missing)}")
    if extra:
        errors.append(f"status.txt unexpected fields: {','.join(extra)}")
    return result


def _read_summary(path: Path, errors: list[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            rows = list(reader)
    except FileNotFoundError:
        errors.append("missing _summary.tsv")
        return []
    except (OSError, csv.Error) as exc:
        errors.append(f"cannot read _summary.tsv: {exc}")
        return []

    if tuple(reader.fieldnames or ()) != SUMMARY_FIELDS:
        errors.append(
            "_summary.tsv header must be: " + "\t".join(SUMMARY_FIELDS)
        )
    if any(None in row for row in rows):
        errors.append("_summary.tsv contains rows with extra columns")
    return rows


def _validate_snapshot(
    payload: dict[str, Any] | None, errors: list[str]
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "snapshot_id": None,
        "captured_at": None,
        "error_node_count": None,
    }
    if payload is None:
        return summary
    if payload.get("status") != "success":
        errors.append("_snapshot.json status must be success")
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        errors.append("_snapshot.json snapshot_id must be 32 hexadecimal characters")
    else:
        summary["snapshot_id"] = snapshot_id.lower()
    captured_at = payload.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        errors.append("_snapshot.json captured_at must be a non-empty string")
    else:
        summary["captured_at"] = captured_at
    error_count = payload.get("error_node_count")
    if not _is_nonnegative_int(error_count):
        errors.append("_snapshot.json error_node_count must be a non-negative integer")
    else:
        summary["error_node_count"] = error_count
    return summary


def _validate_chain(
    chain: Any, mode: str, index: int, errors: list[str]
) -> None:
    prefix = f"{mode}.json chain[{index}]"
    if not isinstance(chain, dict):
        errors.append(f"{prefix} must be an object")
        return
    for field in ("root_cause_kind", "root_cause"):
        value = chain.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{prefix}.{field} must be a non-empty string")
    if not isinstance(chain.get("root_cause_reason"), str):
        errors.append(f"{prefix}.root_cause_reason must be a string")
    affected = chain.get("affected_node_ids")
    if not isinstance(affected, list) or any(
        not isinstance(value, str) or not value for value in affected
    ):
        errors.append(
            f"{prefix}.affected_node_ids must be a list of non-empty strings"
        )
    if "score" in chain and not _is_number(chain["score"]):
        errors.append(f"{prefix}.score must be a finite number")


def _validate_mode(
    payload: dict[str, Any] | None, mode: str, errors: list[str]
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "status": None,
        "snapshot_id": None,
        "snapshot_captured_at": None,
        "error_node_count": None,
        "timing_sec": None,
        "total_chains": None,
        "serialized_chains": None,
        "ai_status": None,
        "ai_text_length": None,
    }
    if payload is None:
        return summary

    if payload.get("status") != "success":
        errors.append(f"{mode}.json status must be success")
    else:
        summary["status"] = "success"
    if payload.get("mode") != mode:
        errors.append(f"{mode}.json mode field must equal {mode}")

    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        errors.append(f"{mode}.json snapshot_id must be 32 hexadecimal characters")
    else:
        summary["snapshot_id"] = snapshot_id.lower()

    captured_at = payload.get("snapshot_captured_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        errors.append(
            f"{mode}.json snapshot_captured_at must be a non-empty string"
        )
    else:
        summary["snapshot_captured_at"] = captured_at

    timing = payload.get("timing_sec")
    if not _is_number(timing) or float(timing) < 0:
        errors.append(f"{mode}.json timing_sec must be a finite non-negative number")
    else:
        summary["timing_sec"] = float(timing)

    error_count = payload.get("error_node_count")
    if not _is_nonnegative_int(error_count):
        errors.append(
            f"{mode}.json error_node_count must be a non-negative integer"
        )
        error_count = None
    else:
        summary["error_node_count"] = error_count

    data = payload.get("data")
    if not isinstance(data, dict):
        errors.append(f"{mode}.json data must be an object")
        return summary

    total_chains = data.get("total_chains")
    chains = data.get("chains")
    if not _is_nonnegative_int(total_chains):
        errors.append(f"{mode}.json data.total_chains must be a non-negative integer")
    else:
        summary["total_chains"] = total_chains
    if not isinstance(chains, list):
        errors.append(f"{mode}.json data.chains must be a list")
        chains = []
    else:
        summary["serialized_chains"] = len(chains)
        for index, chain in enumerate(chains):
            _validate_chain(chain, mode, index, errors)
    if _is_nonnegative_int(total_chains) and total_chains < len(chains):
        errors.append(
            f"{mode}.json total_chains cannot be smaller than serialized chains"
        )
    if error_count == 0:
        if total_chains != 0:
            errors.append(
                f"{mode}.json error_node_count=0 requires total_chains=0"
            )
        if chains:
            errors.append(
                f"{mode}.json error_node_count=0 requires empty chains"
            )

    ai_text = data.get("ai_analysis")
    if not isinstance(ai_text, str):
        errors.append(f"{mode}.json data.ai_analysis must be a string")
        ai_text = ""
    summary["ai_text_length"] = len(ai_text)

    if mode in {"hybrid", "llm_only"}:
        ai_status = data.get("ai_status")
        summary["ai_status"] = ai_status
        if ai_status not in AI_STATUSES:
            errors.append(
                f"{mode}.json data.ai_status must be success or not_applicable"
            )
        if error_count is not None and error_count > 0:
            if ai_status != "success":
                errors.append(
                    f"{mode}.json requires ai_status=success when errors are present"
                )
            if not ai_text.strip():
                errors.append(
                    f"{mode}.json requires non-empty ai_analysis when errors are present"
                )
        if error_count == 0:
            if ai_status != "not_applicable":
                errors.append(
                    f"{mode}.json error_node_count=0 requires "
                    "ai_status=not_applicable"
                )
            if ai_text:
                errors.append(
                    f"{mode}.json error_node_count=0 requires empty ai_analysis"
                )
        if ai_status == "success" and not ai_text.strip():
            errors.append(
                f"{mode}.json ai_status=success requires non-empty ai_analysis"
            )

    if mode == "llm_only":
        if total_chains != 0:
            errors.append("llm_only.json data.total_chains must be 0")
        if chains:
            errors.append("llm_only.json data.chains must be empty")

    return summary


def _validate_cycle(
    cycle_dir: Path,
    cycle_index: int,
    scenarios: list[str],
    ground_truth: dict[str, dict[str, Any]],
    *,
    require_valid: bool,
) -> dict[str, Any]:
    cycle_errors: list[str] = []
    result: dict[str, Any] = {
        "cycle_index": cycle_index,
        "cycle_dir": str(cycle_dir),
        "valid": False,
        "scenario_count": 0,
        "evaluated_scenario_count": 0,
        "excluded_scenario_count": 0,
        "mode_output_count": 0,
        "expected_mode_output_count": 0,
        "errors": cycle_errors,
        "scenarios": [],
    }
    if not cycle_dir.is_dir():
        cycle_errors.append(f"cycle directory does not exist: {cycle_dir}")
        return result

    actual_dirs = sorted(path.name for path in cycle_dir.iterdir() if path.is_dir())
    expected_set = set(scenarios)
    actual_set = set(actual_dirs)
    missing_dirs = [name for name in scenarios if name not in actual_set]
    extra_dirs = sorted(actual_set - expected_set)
    if missing_dirs:
        cycle_errors.append(
            "missing scenario directories: " + ",".join(missing_dirs)
        )
    if extra_dirs:
        cycle_errors.append("unexpected scenario directories: " + ",".join(extra_dirs))
    result["scenario_count"] = len(actual_set & expected_set)

    summary_errors: list[str] = []
    summary_rows = _read_summary(cycle_dir / "_summary.tsv", summary_errors)
    cycle_errors.extend(summary_errors)
    summary_names = [row.get("scenario", "") for row in summary_rows]
    if summary_names != scenarios:
        cycle_errors.append(
            "_summary.tsv scenarios must exactly match manifest order"
        )
    if len(set(summary_names)) != len(summary_names):
        cycle_errors.append("_summary.tsv contains duplicate scenarios")
    summary_by_scenario = {
        row.get("scenario", ""): row
        for row in summary_rows
        if isinstance(row.get("scenario"), str)
    }

    for scenario in scenarios:
        scenario_dir = cycle_dir / scenario
        scenario_errors: list[str] = []
        scenario_result: dict[str, Any] = {
            "scenario": scenario,
            "valid": False,
            "status": None,
            "detectable": ground_truth[scenario]["detectable"],
            "detected": None,
            "snapshot_id": None,
            "error_node_count": None,
            "errors": scenario_errors,
            "modes": {},
        }
        result["scenarios"].append(scenario_result)
        if not scenario_dir.is_dir():
            scenario_errors.append("missing scenario directory")
            continue

        status = _read_status(scenario_dir / "status.txt", scenario_errors)
        scenario_result["status"] = status.get("status")
        scenario_result["detected"] = status.get("detected")
        if status:
            if status.get("scenario") != scenario:
                scenario_errors.append("status.txt scenario does not match directory")
            if status.get("status") not in SCENARIO_STATUSES:
                scenario_errors.append(
                    "status.txt status must be valid or excluded"
                )
            elif require_valid and status.get("status") != "valid":
                scenario_errors.append(
                    "status.txt status must be valid when --require-valid is set"
                )
            expected_detectable = str(ground_truth[scenario]["detectable"]).lower()
            if status.get("detectable") != expected_detectable:
                scenario_errors.append(
                    "status.txt detectable does not match Ground Truth"
                )
            if status.get("detected") not in DETECTED_VALUES:
                scenario_errors.append("status.txt detected has an invalid value")
            if not status.get("reason", "").strip():
                scenario_errors.append("status.txt reason must be non-empty")

        summary_row = summary_by_scenario.get(scenario)
        if summary_row is None:
            scenario_errors.append("scenario is missing from _summary.tsv")
        elif status and any(
            summary_row.get(field) != status.get(field) for field in SUMMARY_FIELDS
        ):
            scenario_errors.append("_summary.tsv row does not match status.txt")

        if status.get("status") == "excluded":
            result["excluded_scenario_count"] += 1
            if status.get("detected") != "not_applicable":
                scenario_errors.append(
                    "excluded status.txt detected must be not_applicable"
                )
            if status.get("reason") != EXCLUDED_REASON:
                scenario_errors.append(
                    f"excluded status.txt reason must be exactly: {EXCLUDED_REASON}"
                )
            unexpected_entries = sorted(
                path.name
                for path in scenario_dir.iterdir()
                if path.name != "status.txt"
            )
            if unexpected_entries:
                scenario_errors.append(
                    "excluded scenario directory must contain only status.txt; "
                    "unexpected entries: " + ",".join(unexpected_entries)
                )
            scenario_result["valid"] = not scenario_errors
            continue

        if status.get("status") == "valid":
            result["evaluated_scenario_count"] += 1

        snapshot_payload = _read_json_object(
            scenario_dir / "_snapshot.json", scenario_errors, "snapshot response"
        )
        snapshot = _validate_snapshot(snapshot_payload, scenario_errors)

        mode_payloads: dict[str, dict[str, Any] | None] = {}
        for mode in REQUIRED_MODES:
            mode_payload = _read_json_object(
                scenario_dir / f"{mode}.json",
                scenario_errors,
                f"{mode} response",
            )
            mode_payloads[mode] = mode_payload
            if mode_payload is not None:
                result["mode_output_count"] += 1
            scenario_result["modes"][mode] = _validate_mode(
                mode_payload, mode, scenario_errors
            )

        snapshot_ids = {
            details["snapshot_id"]
            for details in scenario_result["modes"].values()
            if details["snapshot_id"] is not None
        }
        if len(snapshot_ids) != 1:
            scenario_errors.append("three modes must have the same snapshot_id")
        elif snapshot["snapshot_id"] not in snapshot_ids:
            scenario_errors.append("mode snapshot_id does not match _snapshot.json")
        else:
            scenario_result["snapshot_id"] = next(iter(snapshot_ids))

        error_counts = {
            details["error_node_count"]
            for details in scenario_result["modes"].values()
            if details["error_node_count"] is not None
        }
        if len(error_counts) != 1:
            scenario_errors.append(
                "three modes must have the same error_node_count"
            )
        elif snapshot["error_node_count"] not in error_counts:
            scenario_errors.append(
                "mode error_node_count does not match _snapshot.json"
            )
        else:
            scenario_result["error_node_count"] = next(iter(error_counts))

        captured_values = {
            details["snapshot_captured_at"]
            for details in scenario_result["modes"].values()
            if details["snapshot_captured_at"] is not None
        }
        if len(captured_values) != 1:
            scenario_errors.append(
                "three modes must have the same snapshot_captured_at"
            )
        elif snapshot["captured_at"] not in captured_values:
            scenario_errors.append(
                "mode snapshot_captured_at does not match _snapshot.json captured_at"
            )

        scenario_result["valid"] = not scenario_errors

    result["expected_mode_output_count"] = (
        result["evaluated_scenario_count"] * len(REQUIRED_MODES)
    )
    result["valid"] = (
        not cycle_errors
        and result["scenario_count"] == 20
        and result["mode_output_count"] == result["expected_mode_output_count"]
        and all(item["valid"] for item in result["scenarios"])
    )
    return result


def validate_cycles(
    cycle_dirs: Iterable[Path | str],
    manifest_path: Path | str,
    ground_truth_path: Path | str,
    *,
    require_valid: bool = False,
) -> dict[str, Any]:
    manifest = Path(manifest_path).expanduser().resolve()
    ground_truth_file = Path(ground_truth_path).expanduser().resolve()
    scenarios = load_manifest(manifest)
    ground_truth = load_ground_truth(ground_truth_file, scenarios)
    resolved_cycles = [Path(path).expanduser().resolve() for path in cycle_dirs]
    if not resolved_cycles:
        raise InputError("at least one cycle directory is required")

    global_errors: list[str] = []
    if len(set(resolved_cycles)) != len(resolved_cycles):
        global_errors.append("cycle directories must be unique")

    cycles = [
        _validate_cycle(
            path,
            index,
            scenarios,
            ground_truth,
            require_valid=require_valid,
        )
        for index, path in enumerate(resolved_cycles, 1)
    ]
    error_count = len(global_errors) + sum(
        len(cycle["errors"])
        + sum(len(scenario["errors"]) for scenario in cycle["scenarios"])
        for cycle in cycles
    )
    report = {
        "schema_version": "1.0",
        "valid": not global_errors and all(cycle["valid"] for cycle in cycles),
        "manifest": str(manifest),
        "ground_truth": str(ground_truth_file),
        "required_modes": list(REQUIRED_MODES),
        "require_valid": require_valid,
        "global_errors": global_errors,
        "totals": {
            "cycle_count": len(cycles),
            "expected_scenarios_per_cycle": 20,
            "scenario_records": sum(
                cycle["scenario_count"] for cycle in cycles
            ),
            "evaluated_scenario_records": sum(
                cycle["evaluated_scenario_count"] for cycle in cycles
            ),
            "excluded_scenario_records": sum(
                cycle["excluded_scenario_count"] for cycle in cycles
            ),
            "mode_outputs": sum(cycle["mode_output_count"] for cycle in cycles),
            "expected_mode_outputs": sum(
                cycle["expected_mode_output_count"] for cycle in cycles
            ),
            "nominal_mode_outputs": len(cycles) * 20 * len(REQUIRED_MODES),
            "error_count": error_count,
        },
        "cycles": cycles,
    }
    return report


def write_json_report(report: dict[str, Any], path: Path | str) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_tsv_report(report: dict[str, Any], path: Path | str) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "cycle_index",
        "cycle_dir",
        "scenario",
        "valid",
        "status",
        "detectable",
        "detected",
        "snapshot_id",
        "error_node_count",
        "hybrid_timing_sec",
        "dfs_only_timing_sec",
        "llm_only_timing_sec",
        "errors",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        for cycle in report.get("cycles", []):
            if not cycle["scenarios"]:
                writer.writerow(
                    {
                        "cycle_index": cycle["cycle_index"],
                        "cycle_dir": cycle["cycle_dir"],
                        "valid": str(cycle["valid"]).lower(),
                        "errors": " | ".join(cycle["errors"]),
                    }
                )
                continue
            for scenario in cycle["scenarios"]:
                modes = scenario["modes"]
                writer.writerow(
                    {
                        "cycle_index": cycle["cycle_index"],
                        "cycle_dir": cycle["cycle_dir"],
                        "scenario": scenario["scenario"],
                        "valid": str(scenario["valid"]).lower(),
                        "status": scenario["status"],
                        "detectable": str(scenario["detectable"]).lower(),
                        "detected": scenario["detected"],
                        "snapshot_id": scenario["snapshot_id"],
                        "error_node_count": scenario["error_node_count"],
                        "hybrid_timing_sec": modes.get("hybrid", {}).get(
                            "timing_sec"
                        ),
                        "dfs_only_timing_sec": modes.get("dfs_only", {}).get(
                            "timing_sec"
                        ),
                        "llm_only_timing_sec": modes.get("llm_only", {}).get(
                            "timing_sec"
                        ),
                        "errors": " | ".join(scenario["errors"]),
                    }
                )


def build_parser() -> argparse.ArgumentParser:
    workspace = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Validate one or more KUBEIN ablation cycle directories and write "
            "machine-readable JSON and TSV reports."
        )
    )
    parser.add_argument(
        "--cycle-dir",
        dest="cycle_dirs",
        action="append",
        required=True,
        type=Path,
        help="Cycle result directory; repeat for multiple cycles.",
    )
    parser.add_argument(
        "--modes",
        default=",".join(REQUIRED_MODES),
        help="Required comma-separated modes (canonical value: hybrid,dfs_only,llm_only).",
    )
    parser.add_argument(
        "--require-valid",
        action="store_true",
        help="Require every scenario status to be valid (canonical experiment gate).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=workspace / "eval" / "scenarios.txt",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=workspace / "eval" / "ground_truth.json",
    )
    parser.add_argument(
        "--output-json",
        "--json-out",
        dest="output_json",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--output-tsv",
        "--tsv-out",
        dest="output_tsv",
        type=Path,
        default=None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested_modes = tuple(
        value.strip() for value in args.modes.split(",") if value.strip()
    )
    if requested_modes != REQUIRED_MODES:
        print(
            "[ERROR] --modes must be exactly hybrid,dfs_only,llm_only",
            file=sys.stderr,
        )
        return 2
    try:
        report = validate_cycles(
            args.cycle_dirs,
            args.manifest,
            args.ground_truth,
            require_valid=args.require_valid,
        )
    except InputError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.output_json is not None:
        write_json_report(report, args.output_json)
    if args.output_tsv is not None:
        write_tsv_report(report, args.output_tsv)
    state = "PASS" if report["valid"] else "FAIL"
    print(
        f"[{state}] cycles={report['totals']['cycle_count']} "
        f"scenarios={report['totals']['scenario_records']} "
        f"evaluated={report['totals']['evaluated_scenario_records']} "
        f"excluded={report['totals']['excluded_scenario_records']} "
        f"mode_outputs={report['totals']['mode_outputs']}/"
        f"{report['totals']['expected_mode_outputs']} "
        f"errors={report['totals']['error_count']}"
    )
    if args.output_json is not None:
        print(f"JSON: {args.output_json}")
    if args.output_tsv is not None:
        print(f"TSV:  {args.output_tsv}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
