#!/usr/bin/env python3
"""Unit tests for the stdlib-only ablation validation and scoring tools."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


EVAL_DIR = Path(__file__).resolve().parent


def load_script_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, EVAL_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_script_module(
    "validate-ablation-results.py", "test_validate_ablation_results"
)
SCORER = load_script_module(
    "score-ablation-results.py", "test_score_ablation_results"
)


class SyntheticAblationFixture:
    def __init__(self, root: Path):
        self.root = root
        self.manifest = root / "scenarios.txt"
        self.ground_truth = root / "ground_truth.json"
        self.cycle = root / "cycle-1"
        self.scenarios = [f"{index:02d}-scenario" for index in range(1, 21)]
        self._create()

    @staticmethod
    def _chain(kind: str, name: str, reason: str) -> dict:
        return {
            "root_cause_kind": kind,
            "root_cause": name,
            "root_cause_reason": reason,
            "score": 10.0,
            "affected_node_ids": [f"{kind}/sock-shop/{name}"],
        }

    def _ground_truth_payload(self) -> dict:
        payload = {}
        for scenario in self.scenarios:
            payload[scenario] = {
                "detectable": False,
                "description": scenario,
                "expected_rc_kind": None,
                "expected_rc_name_contains": None,
                "expected_rc_reason": None,
                "expected_affected": [],
            }
        payload[self.scenarios[0]].update(
            {
                "detectable": True,
                "expected_rc_kind": ["ReplicaSet", "Service"],
                "expected_rc_name_contains": ["payment", "user"],
                "expected_rc_reason": ["ReplicasMismatch", "NoEndpoints"],
            }
        )
        payload[self.scenarios[1]].update(
            {
                "detectable": True,
                "expected_rc_kind": "Deployment",
                "expected_rc_name_contains": "catalogue",
                "expected_rc_reason": "NoAvailableReplicas",
            }
        )
        return payload

    def _mode_payload(
        self,
        mode: str,
        snapshot_id: str,
        captured_at: str,
        error_count: int,
        chains: list[dict],
    ) -> dict:
        if mode == "llm_only":
            chains = []
        has_errors = error_count > 0
        data = {
            "total_chains": 0 if mode == "llm_only" else len(chains),
            "chains": chains,
            "ai_analysis": (
                f"{mode} synthetic analysis" if has_errors and mode != "dfs_only" else ""
            ),
            "graph_data": {"nodes": [], "edges": [], "dfs_path": []},
        }
        if mode in {"hybrid", "llm_only"}:
            data["ai_status"] = "success" if has_errors else "not_applicable"
        return {
            "status": "success",
            "mode": mode,
            "timing_sec": {"hybrid": 1.25, "dfs_only": 0.25, "llm_only": 0.75}[
                mode
            ],
            "error_node_count": error_count,
            "snapshot_id": snapshot_id,
            "snapshot_captured_at": captured_at,
            "data": data,
        }

    def _create(self) -> None:
        self.manifest.write_text("\n".join(self.scenarios) + "\n", encoding="utf-8")
        self.ground_truth.write_text(
            json.dumps(self._ground_truth_payload(), indent=2),
            encoding="utf-8",
        )
        self.cycle.mkdir()
        summary_rows = []
        for index, scenario in enumerate(self.scenarios, 1):
            scenario_dir = self.cycle / scenario
            scenario_dir.mkdir()
            detectable = index <= 2
            detected = "true" if detectable else "not_applicable"
            status = {
                "scenario": scenario,
                "status": "valid",
                "detectable": str(detectable).lower(),
                "detected": detected,
                "reason": "completed",
            }
            (scenario_dir / "status.txt").write_text(
                "".join(f"{key}={value}\n" for key, value in status.items()),
                encoding="utf-8",
            )
            summary_rows.append(status)

            snapshot_id = f"{index:032x}"
            captured_at = f"2026-07-29T00:00:{index:02d}Z"
            error_count = 2 if index == 1 else (1 if index == 2 else 0)
            snapshot = {
                "status": "success",
                "snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "expires_in_sec": 300,
                "error_node_count": error_count,
            }
            (scenario_dir / "_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8"
            )

            if index == 1:
                hybrid_chains = [
                    self._chain(
                        "ReplicaSet", "payment-abc", "ReplicasMismatch"
                    ),
                    self._chain("Service", "user", "NoEndpoints"),
                ]
                # Both pairs are deliberately crossed. A Cartesian matcher would
                # incorrectly count them as hits.
                dfs_chains = [
                    self._chain("ReplicaSet", "user", "ReplicasMismatch"),
                    self._chain("Service", "payment", "NoEndpoints"),
                ]
            elif index == 2:
                hybrid_chains = [
                    self._chain(
                        "Deployment", "catalogue", "NoAvailableReplicas"
                    )
                ]
                dfs_chains = list(hybrid_chains)
            else:
                hybrid_chains = []
                dfs_chains = []

            mode_chains = {
                "hybrid": hybrid_chains,
                "dfs_only": dfs_chains,
                "llm_only": [],
            }
            for mode in ("hybrid", "dfs_only", "llm_only"):
                payload = self._mode_payload(
                    mode,
                    snapshot_id,
                    captured_at,
                    error_count,
                    mode_chains[mode],
                )
                (scenario_dir / f"{mode}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

        with (self.cycle / "_summary.tsv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=list(VALIDATOR.SUMMARY_FIELDS),
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(summary_rows)

    def mutate_json(self, relative_path: str, callback) -> None:
        path = self.cycle / relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        callback(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def set_status(self, scenario_index: int, **updates: str) -> None:
        scenario = self.scenarios[scenario_index]
        status_path = self.cycle / scenario / "status.txt"
        status = dict(
            line.split("=", 1)
            for line in status_path.read_text(encoding="utf-8").splitlines()
        )
        status.update(updates)
        status_path.write_text(
            "".join(
                f"{field}={status[field]}\n"
                for field in VALIDATOR.STATUS_FIELDS
            ),
            encoding="utf-8",
        )

        summary_path = self.cycle / "_summary.tsv"
        with summary_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        rows[scenario_index].update(updates)
        with summary_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=list(VALIDATOR.SUMMARY_FIELDS),
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)

    def exclude_scenario(self, scenario_index: int) -> None:
        scenario_dir = self.cycle / self.scenarios[scenario_index]
        self.set_status(
            scenario_index,
            status="excluded",
            detected="not_applicable",
            reason=VALIDATOR.EXCLUDED_REASON,
        )
        for path in scenario_dir.iterdir():
            if path.name != "status.txt":
                path.unlink()


class AblationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.fixture = SyntheticAblationFixture(Path(self.temp_dir.name))

    def validate(self) -> dict:
        return VALIDATOR.validate_cycles(
            [self.fixture.cycle],
            self.fixture.manifest,
            self.fixture.ground_truth,
        )

    def scenario_errors(self, report: dict, scenario_index: int = 0) -> str:
        return "\n".join(
            report["cycles"][0]["scenarios"][scenario_index]["errors"]
        )

    def test_valid_cycle_and_validator_cli_outputs(self) -> None:
        report = self.validate()
        self.assertTrue(report["valid"])
        self.assertEqual(report["totals"]["scenario_records"], 20)
        self.assertEqual(report["totals"]["mode_outputs"], 60)

        json_out = Path(self.temp_dir.name) / "validation.json"
        tsv_out = Path(self.temp_dir.name) / "validation.tsv"
        exit_code = VALIDATOR.main(
            [
                "--manifest",
                str(self.fixture.manifest),
                "--ground-truth",
                str(self.fixture.ground_truth),
                "--cycle-dir",
                str(self.fixture.cycle),
                "--modes",
                "hybrid,dfs_only,llm_only",
                "--require-valid",
                "--output-json",
                str(json_out),
                "--output-tsv",
                str(tsv_out),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(json_out.is_file())
        self.assertEqual(len(tsv_out.read_text(encoding="utf-8").splitlines()), 21)

    def test_missing_mode_is_rejected(self) -> None:
        (self.fixture.cycle / self.fixture.scenarios[0] / "llm_only.json").unlink()
        report = self.validate()
        self.assertFalse(report["valid"])
        self.assertIn("missing llm_only response", self.scenario_errors(report))
        self.assertEqual(report["totals"]["mode_outputs"], 59)

    def test_snapshot_and_error_count_mismatch_are_rejected(self) -> None:
        scenario = self.fixture.scenarios[0]

        def mutate(payload: dict) -> None:
            payload["snapshot_id"] = "f" * 32
            payload["error_node_count"] = 99

        self.fixture.mutate_json(f"{scenario}/dfs_only.json", mutate)
        report = self.validate()
        errors = self.scenario_errors(report)
        self.assertFalse(report["valid"])
        self.assertIn("same snapshot_id", errors)
        self.assertIn("same error_node_count", errors)

    def test_empty_llm_text_with_errors_is_rejected(self) -> None:
        scenario = self.fixture.scenarios[0]
        self.fixture.mutate_json(
            f"{scenario}/llm_only.json",
            lambda payload: payload["data"].update({"ai_analysis": ""}),
        )
        report = self.validate()
        self.assertFalse(report["valid"])
        self.assertIn("requires non-empty ai_analysis", self.scenario_errors(report))

    def test_zero_error_ai_output_is_rejected(self) -> None:
        scenario_index = 2
        scenario = self.fixture.scenarios[scenario_index]
        self.fixture.mutate_json(
            f"{scenario}/llm_only.json",
            lambda payload: payload["data"].update(
                {
                    "ai_status": "success",
                    "ai_analysis": "unexpected provider output",
                }
            ),
        )
        report = self.validate()
        errors = self.scenario_errors(report, scenario_index)
        self.assertFalse(report["valid"])
        self.assertIn(
            "error_node_count=0 requires ai_status=not_applicable",
            errors,
        )
        self.assertIn(
            "error_node_count=0 requires empty ai_analysis",
            errors,
        )

    def test_zero_error_chains_are_rejected(self) -> None:
        scenario_index = 2
        scenario = self.fixture.scenarios[scenario_index]
        unexpected_chain = self.fixture._chain(
            "Deployment", "unexpected", "UnexpectedRootCause"
        )
        self.fixture.mutate_json(
            f"{scenario}/hybrid.json",
            lambda payload: payload["data"].update(
                {"total_chains": 1, "chains": [unexpected_chain]}
            ),
        )
        report = self.validate()
        errors = self.scenario_errors(report, scenario_index)
        self.assertFalse(report["valid"])
        self.assertIn(
            "error_node_count=0 requires total_chains=0",
            errors,
        )
        self.assertIn(
            "error_node_count=0 requires empty chains",
            errors,
        )

    def test_invalid_status_is_rejected(self) -> None:
        scenario = self.fixture.scenarios[0]
        self.fixture.set_status(0, status="invalid")
        report = self.validate()
        self.assertFalse(report["valid"])
        self.assertIn(
            "status.txt status must be valid or excluded",
            self.scenario_errors(report),
        )

    def test_excluded_status_is_valid_by_default_and_changes_denominators(
        self,
    ) -> None:
        self.fixture.exclude_scenario(0)

        report = self.validate()

        self.assertTrue(report["valid"])
        self.assertEqual(report["totals"]["scenario_records"], 20)
        self.assertEqual(report["totals"]["evaluated_scenario_records"], 19)
        self.assertEqual(report["totals"]["excluded_scenario_records"], 1)
        self.assertEqual(report["totals"]["mode_outputs"], 57)
        self.assertEqual(report["totals"]["expected_mode_outputs"], 57)
        self.assertEqual(report["totals"]["nominal_mode_outputs"], 60)
        excluded = report["cycles"][0]["scenarios"][0]
        self.assertEqual(excluded["status"], "excluded")
        self.assertEqual(excluded["detected"], "not_applicable")
        self.assertEqual(excluded["modes"], {})

    def test_require_valid_rejects_excluded_status(self) -> None:
        self.fixture.exclude_scenario(0)

        report = VALIDATOR.validate_cycles(
            [self.fixture.cycle],
            self.fixture.manifest,
            self.fixture.ground_truth,
            require_valid=True,
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "status.txt status must be valid when --require-valid is set",
            self.scenario_errors(report),
        )

    def test_excluded_status_requires_exact_detected_and_reason(self) -> None:
        self.fixture.exclude_scenario(0)
        self.fixture.set_status(0, detected="not_run", reason="manually skipped")

        report = self.validate()
        errors = self.scenario_errors(report)

        self.assertFalse(report["valid"])
        self.assertIn(
            "excluded status.txt detected must be not_applicable", errors
        )
        self.assertIn(
            "excluded status.txt reason must be exactly", errors
        )

    def test_excluded_directory_rejects_any_extra_artifact(self) -> None:
        self.fixture.exclude_scenario(0)
        scenario_dir = self.fixture.cycle / self.fixture.scenarios[0]
        (scenario_dir / "hybrid.failed.json").write_text(
            "{}", encoding="utf-8"
        )

        report = self.validate()

        self.assertFalse(report["valid"])
        self.assertIn(
            "excluded scenario directory must contain only status.txt",
            self.scenario_errors(report),
        )

    def test_index_wise_matching_rejects_crossed_multi_fault_pairs(self) -> None:
        report = SCORER.score_cycles(
            [self.fixture.cycle],
            self.fixture.manifest,
            self.fixture.ground_truth,
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["totals"]["mode_response_count"], 60)
        scenario = self.fixture.scenarios[0]
        hybrid = next(
            row
            for row in report["details"]
            if row["scenario"] == scenario and row["mode"] == "hybrid"
        )
        dfs = next(
            row
            for row in report["details"]
            if row["scenario"] == scenario and row["mode"] == "dfs_only"
        )
        self.assertEqual(hybrid["matched_expected"], 2)
        self.assertTrue(hybrid["exact_set"])
        self.assertEqual(dfs["matched_expected"], 0)
        self.assertFalse(dfs["hit_at_1"])
        self.assertEqual(report["overall"]["hybrid"]["hit_at_1_rate"], 1.0)
        self.assertEqual(report["overall"]["dfs_only"]["hit_at_1_rate"], 0.5)
        self.assertEqual(
            report["overall"]["llm_only"]["policy"], "integrity_only"
        )
        self.assertIsNone(
            report["overall"]["llm_only"]["content_accuracy"]
        )

    def test_reason_is_part_of_the_tuple_and_reported_separately(self) -> None:
        scenario = self.fixture.scenarios[1]

        def mutate(payload: dict) -> None:
            payload["data"]["chains"][0]["root_cause_reason"] = "Unavailable"

        self.fixture.mutate_json(f"{scenario}/dfs_only.json", mutate)
        report = SCORER.score_cycles(
            [self.fixture.cycle],
            self.fixture.manifest,
            self.fixture.ground_truth,
        )
        dfs = next(
            row
            for row in report["details"]
            if row["scenario"] == scenario and row["mode"] == "dfs_only"
        )
        self.assertEqual(dfs["matched_expected"], 0)
        self.assertEqual(dfs["identity_match_count_without_reason"], 1)
        self.assertEqual(dfs["reason_mismatch_count"], 1)

    def test_scorer_reports_excluded_as_not_run_without_metric_pollution(
        self,
    ) -> None:
        self.fixture.exclude_scenario(0)

        report = SCORER.score_cycles(
            [self.fixture.cycle],
            self.fixture.manifest,
            self.fixture.ground_truth,
        )

        self.assertEqual(report["totals"]["scenario_count"], 20)
        self.assertEqual(report["totals"]["mode_response_count"], 57)
        self.assertEqual(report["totals"]["detail_row_count"], 60)
        self.assertEqual(
            report["coverage"],
            {
                "nominal_scenario_count": 20,
                "evaluated_scenario_count": 19,
                "excluded_scenario_count": 1,
                "scenario_coverage_rate": 0.95,
                "nominal_mode_count": 60,
                "evaluated_mode_response_count": 57,
                "excluded_mode_count": 3,
            },
        )
        self.assertEqual(
            report["cycle_aggregates"][0]["coverage"], report["coverage"]
        )
        for mode in ("hybrid", "dfs_only"):
            aggregate = report["overall"][mode]
            self.assertEqual(aggregate["trial_count"], 19)
            self.assertEqual(aggregate["detectable_trial_count"], 1)
            self.assertEqual(
                aggregate["detection_confusion"]["fn"], 0
            )
            self.assertEqual(
                aggregate["detection_confusion"]["accuracy"], 1.0
            )
            self.assertEqual(aggregate["timing"]["n"], 19)
        self.assertEqual(report["overall"]["llm_only"]["trial_count"], 19)
        self.assertEqual(report["overall"]["llm_only"]["timing"]["n"], 19)

        excluded_rows = [
            row
            for row in report["details"]
            if row["scenario"] == self.fixture.scenarios[0]
        ]
        self.assertEqual(len(excluded_rows), 3)
        self.assertTrue(
            all(row["evaluation_status"] == "not_run" for row in excluded_rows)
        )
        self.assertTrue(
            all(row["response_integrity"] is None for row in excluded_rows)
        )
        self.assertTrue(all(row["timing_sec"] is None for row in excluded_rows))

    def test_scorer_require_valid_rejects_excluded_status(self) -> None:
        self.fixture.exclude_scenario(0)

        with self.assertRaisesRegex(
            SCORER.ScoringError, "--require-valid"
        ):
            SCORER.score_cycles(
                [self.fixture.cycle],
                self.fixture.manifest,
                self.fixture.ground_truth,
                require_valid=True,
            )

    def test_scorer_cli_writes_json_and_tsv(self) -> None:
        json_out = Path(self.temp_dir.name) / "scores.json"
        tsv_out = Path(self.temp_dir.name) / "scores.tsv"
        exit_code = SCORER.main(
            [
                "--ground-truth",
                str(self.fixture.ground_truth),
                "--manifest",
                str(self.fixture.manifest),
                "--cycle-dir",
                str(self.fixture.cycle),
                "--output-json",
                str(json_out),
                "--output-tsv",
                str(tsv_out),
                "--require-valid",
            ]
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(payload["scoring_policy"]["llm_only"], "integrity_only")
        tsv_lines = tsv_out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(tsv_lines), 1 + 60 + 1 + 3)
        self.assertIn("coverage", tsv_lines[-4])
        self.assertIn("aggregate", tsv_lines[-1])


if __name__ == "__main__":
    unittest.main()
