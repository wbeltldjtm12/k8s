"""Static safety contracts for the cluster ablation runners.

These tests deliberately inspect the shell source instead of contacting a
cluster.  They guard the recovery and result-integrity hooks that make a long,
unattended experiment safe to restart.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PRECHECK_PATH = REPO_ROOT / "cluster-port" / "scripts" / "03-pre-check-cluster.sh"
EVAL_PATH = REPO_ROOT / "cluster-port" / "scripts" / "04-ablation-eval-cluster.sh"
FIVE_CYCLE_PATH = (
    REPO_ROOT / "cluster-port" / "scripts" / "05-ablation-5cycle-cluster.sh"
)
SCENARIO_LIB_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "lib" / "scenario-lib.sh"
)
DNS_PREPARE_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "11-dns-error" / "prepare.sh"
)
DNS_RECOVER_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "11-dns-error" / "recover.sh"
)
DNS_FAULT_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "11-dns-error" / "fault.yaml"
)
IO_PREPARE_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "15-io-delay" / "prepare.sh"
)
IO_RECOVER_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "15-io-delay" / "recover.sh"
)
IO_FAULT_PATH = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "15-io-delay" / "fault.yaml"
)
POD_FAILURE_DIR = (
    REPO_ROOT / "cluster-port" / "sock-shop-scenarios" / "02-pod-failure"
)
POD_FAILURE_FAULT_PATH = POD_FAILURE_DIR / "fault.yaml"
POD_FAILURE_RECOVER_PATH = POD_FAILURE_DIR / "recover.sh"
POD_FAILURE_LEGACY_INJECT_PATH = (
    POD_FAILURE_DIR / "legacy-scale-to-zero.inject.sh.disabled"
)
POD_FAILURE_LEGACY_RECOVER_PATH = (
    POD_FAILURE_DIR / "legacy-scale-to-zero.recover.sh.disabled"
)
GROUND_TRUTH_PATH = REPO_ROOT / "eval" / "ground_truth.json"
SCENARIO_MANIFEST_PATH = REPO_ROOT / "eval" / "scenarios.txt"
SCENARIO_METADATA_VALIDATOR_PATH = (
    REPO_ROOT / "eval" / "validate-scenario-metadata.py"
)
SCENARIO_METADATA_PATH = (
    REPO_ROOT
    / "cluster-port"
    / "sock-shop-scenarios"
    / "scenario-metadata.json"
)
ENV_EXAMPLE_PATH = REPO_ROOT / "cluster-port" / ".env.cluster.example"
BACKEND_MAIN_PATH = REPO_ROOT / "backend" / "main.py"
METRICS_PATH = REPO_ROOT / "backend" / "infra" / "metrics.py"


def read_script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\)\s*\{{\s*\n(?P<body>.*?)^\}}",
        source,
    )
    if match is None:
        raise AssertionError(f"missing shell function: {name}")
    return match.group("body")


def bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    converted = subprocess.run(
        ["wsl.exe", "wslpath", "-a", resolved.as_posix()],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return converted.stdout.strip()


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        raise unittest.SkipTest("bash is required for shell behavior contracts")
    with tempfile.TemporaryDirectory() as temp_dir:
        script_path = Path(temp_dir) / "contract.sh"
        script_path.write_text(script, encoding="utf-8", newline="\n")
        return subprocess.run(
            [bash, bash_path(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )


def deployment_item(name: str, *, converged: bool = True) -> dict[str, object]:
    ready = 1 if converged else 0
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "generation": 9},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 9,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": ready,
            "availableReplicas": ready,
            "unavailableReplicas": 0 if converged else 1,
        },
    }


def pod_item(
    name: str,
    *,
    ready: bool = True,
    terminating: bool = False,
) -> dict[str, object]:
    metadata = {"name": name}
    if terminating:
        metadata["deletionTimestamp"] = "2026-07-30T00:00:00Z"
    return {
        "kind": "Pod",
        "metadata": metadata,
        "status": {
            "phase": "Running" if ready else "Pending",
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"}
            ],
            "containerStatuses": [{"name": "app", "ready": ready}],
        },
    }


def kubernetes_list(*items: dict[str, object]) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})


class RunnerSafetyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.precheck = read_script(PRECHECK_PATH)
        cls.eval_runner = read_script(EVAL_PATH)
        cls.five_cycle = read_script(FIVE_CYCLE_PATH)
        cls.scenario_lib = read_script(SCENARIO_LIB_PATH)
        cls.dns_prepare = read_script(DNS_PREPARE_PATH)
        cls.dns_recover = read_script(DNS_RECOVER_PATH)
        cls.dns_fault = read_script(DNS_FAULT_PATH)
        cls.io_prepare = read_script(IO_PREPARE_PATH)
        cls.io_recover = read_script(IO_RECOVER_PATH)
        cls.io_fault = read_script(IO_FAULT_PATH)
        cls.pod_failure_fault = read_script(POD_FAILURE_FAULT_PATH)
        cls.pod_failure_recover = read_script(POD_FAILURE_RECOVER_PATH)
        cls.pod_failure_legacy_inject = read_script(
            POD_FAILURE_LEGACY_INJECT_PATH
        )
        cls.pod_failure_legacy_recover = read_script(
            POD_FAILURE_LEGACY_RECOVER_PATH
        )
        cls.ground_truth = json.loads(
            GROUND_TRUTH_PATH.read_text(encoding="utf-8")
        )
        cls.scenario_metadata = json.loads(
            SCENARIO_METADATA_PATH.read_text(encoding="utf-8")
        )
        cls.env_example = read_script(ENV_EXAMPLE_PATH)
        cls.backend_main = read_script(BACKEND_MAIN_PATH)
        cls.metrics_source = read_script(METRICS_PATH)

    def test_oom_gt_requires_the_direct_container_failure(self) -> None:
        entry = self.ground_truth["05-oomkill"]
        self.assertTrue(entry["detectable"])
        self.assertEqual(entry["expected_rc_kind"], "Pod")
        self.assertEqual(entry["expected_rc_name_contains"], "orders")
        self.assertEqual(entry["expected_rc_reason"], "OOMKilled")

    def test_pod_failure_gt_matches_repeatable_podchaos_signal(self) -> None:
        entry = self.ground_truth["02-pod-failure"]
        self.assertTrue(entry["detectable"])
        self.assertEqual(entry["expected_rc_kind"], "Deployment")
        self.assertEqual(entry["expected_rc_name_contains"], "catalogue")
        self.assertEqual(
            entry["expected_rc_reason"],
            "NoAvailableReplicas",
        )

    def test_liveness_gt_matches_repeatable_replicaset_signal(self) -> None:
        entry = self.ground_truth["07-liveness-probe"]
        self.assertTrue(entry["detectable"])
        self.assertEqual(entry["expected_rc_kind"], "ReplicaSet")
        self.assertEqual(entry["expected_rc_name_contains"], "catalogue")
        self.assertEqual(entry["expected_rc_reason"], "ReplicasMismatch")

    def test_bad_pvc_gt_matches_missing_dependency_contract(self) -> None:
        entry = self.ground_truth["10-bad-pvc"]
        self.assertTrue(entry["detectable"])
        self.assertEqual(entry["expected_rc_kind"], "PVC")
        self.assertEqual(entry["expected_rc_reason"], "Missing")

    def test_precheck_detectable_count_matches_ground_truth(self) -> None:
        assignment = re.search(
            r'(?m)^EXPECTED_DETECTABLE_COUNT='
            r'"\$\{EXPECTED_DETECTABLE_COUNT:-([0-9]+)\}"$',
            self.precheck,
        )
        self.assertIsNotNone(
            assignment,
            "precheck must expose the expected detectable-count contract",
        )
        expected = int(assignment.group(1))
        actual = sum(
            int(entry["detectable"])
            for entry in self.ground_truth.values()
        )
        self.assertEqual(expected, actual)
        self.assertIn(
            "detectable_count != expected_detectable_count",
            self.precheck,
        )

    def test_canonical_scenario_metadata_strictly_resolves_all_scenarios(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCENARIO_METADATA_VALIDATOR_PATH),
                "--metadata",
                str(SCENARIO_METADATA_PATH),
                "--manifest",
                str(SCENARIO_MANIFEST_PATH),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        resolved = {
            parts[0]: parts[1:]
            for line in completed.stdout.splitlines()
            if (parts := line.split("\t"))
        }
        self.assertEqual(20, len(resolved))
        self.assertEqual(["5", "12"], resolved["12-network-delay"])
        self.assertEqual(
            5,
            self.scenario_metadata["scenarios"]["12-network-delay"][
                "observation_settle_seconds"
            ],
        )
        self.assertEqual(
            12,
            self.scenario_metadata["scenarios"]["12-network-delay"][
                "recovery_settle_seconds"
            ],
        )
        self.assertFalse(self.ground_truth["12-network-delay"]["detectable"])

        with tempfile.TemporaryDirectory() as tmp:
            override_document = json.loads(
                json.dumps(self.scenario_metadata)
            )
            override_document["scenarios"]["12-network-delay"][
                "observation_settle_seconds"
            ] = 7
            override_path = Path(tmp) / "metadata.json"
            override_path.write_text(
                json.dumps(override_document),
                encoding="utf-8",
            )
            override_run = subprocess.run(
                [
                    sys.executable,
                    str(SCENARIO_METADATA_VALIDATOR_PATH),
                    "--metadata",
                    str(override_path),
                    "--manifest",
                    str(SCENARIO_MANIFEST_PATH),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            self.assertEqual(0, override_run.returncode)
            override_resolved = {
                parts[0]: parts[1:]
                for line in override_run.stdout.splitlines()
                if (parts := line.split("\t"))
            }
            self.assertEqual(
                ["7", "12"],
                override_resolved["12-network-delay"],
            )
            self.assertEqual(
                ["5", "0"],
                override_resolved["11-dns-error"],
            )

    def test_scenario_metadata_rejects_unsafe_values_and_unknown_keys(
        self,
    ) -> None:
        invalid_documents = []
        for value in (-1, 21, True, 5.5, "5"):
            document = json.loads(json.dumps(self.scenario_metadata))
            document["defaults"]["observation_settle_seconds"] = value
            invalid_documents.append((f"value={value!r}", document))

        invalid_recovery = json.loads(json.dumps(self.scenario_metadata))
        invalid_recovery["scenarios"]["12-network-delay"][
            "recovery_settle_seconds"
        ] = 21
        invalid_documents.append(("unsafe recovery settle", invalid_recovery))

        unknown_top = json.loads(json.dumps(self.scenario_metadata))
        unknown_top["unexpected"] = {}
        invalid_documents.append(("unknown top-level key", unknown_top))

        boolean_schema = json.loads(json.dumps(self.scenario_metadata))
        boolean_schema["schema_version"] = True
        invalid_documents.append(("boolean schema version", boolean_schema))

        float_schema = json.loads(json.dumps(self.scenario_metadata))
        float_schema["schema_version"] = 1.0
        invalid_documents.append(("float schema version", float_schema))

        unknown_id = json.loads(json.dumps(self.scenario_metadata))
        unknown_id["scenarios"]["99-unknown"] = {
            "observation_settle_seconds": 5
        }
        invalid_documents.append(("unknown scenario ID", unknown_id))

        unknown_key = json.loads(json.dumps(self.scenario_metadata))
        unknown_key["scenarios"]["12-network-delay"]["sleep_seconds"] = 5
        invalid_documents.append(("unknown scenario key", unknown_key))

        for label, document in invalid_documents:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                metadata_path = Path(tmp) / "metadata.json"
                metadata_path.write_text(
                    json.dumps(document),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCENARIO_METADATA_VALIDATOR_PATH),
                        "--metadata",
                        str(metadata_path),
                        "--manifest",
                        str(SCENARIO_MANIFEST_PATH),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn("[ERROR]", completed.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            duplicate_path = Path(tmp) / "duplicate.json"
            duplicate_path.write_text(
                '{"schema_version":1,"schema_version":1,'
                '"defaults":{"observation_settle_seconds":5,'
                '"recovery_settle_seconds":0},'
                '"scenarios":{}}',
                encoding="utf-8",
            )
            duplicate_run = subprocess.run(
                [
                    sys.executable,
                    str(SCENARIO_METADATA_VALIDATOR_PATH),
                    "--metadata",
                    str(duplicate_path),
                    "--manifest",
                    str(SCENARIO_MANIFEST_PATH),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            self.assertEqual(2, duplicate_run.returncode)
            self.assertIn("duplicate JSON key", duplicate_run.stderr)

    def test_scenario_metadata_rejects_manifest_path_traversal(self) -> None:
        scenarios = SCENARIO_MANIFEST_PATH.read_text(
            encoding="utf-8"
        ).splitlines()
        scenarios[0] = "../../outside"
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "scenarios.txt"
            manifest_path.write_text(
                "\n".join(scenarios) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCENARIO_METADATA_VALIDATOR_PATH),
                    "--metadata",
                    str(SCENARIO_METADATA_PATH),
                    "--manifest",
                    str(manifest_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        self.assertEqual(2, completed.returncode)
        self.assertIn("invalid IDs", completed.stderr)

    def test_settle_metadata_is_validated_before_injection_and_recorded(
        self,
    ) -> None:
        validator_call = self.eval_runner.find(
            'python3 "$SCENARIO_METADATA_VALIDATOR"'
        )
        injection = self.eval_runner.find('inject_scenario "$scen"')
        settle_log = self.eval_runner.find(
            'observation settle ${settle_seconds}초'
        )
        settle_sleep = self.eval_runner.find(
            'sleep "$settle_seconds"',
            settle_log,
        )
        detection = self.eval_runner.find(
            'if [ "$detectable" = "false" ]',
            settle_sleep,
        )
        self.assertGreaterEqual(validator_call, 0)
        self.assertGreater(injection, validator_call)
        self.assertGreater(settle_log, injection)
        self.assertGreater(settle_sleep, settle_log)
        self.assertGreater(
            detection,
            settle_sleep,
            "resolved settle must precede detection/evidence/snapshot",
        )
        evidence = function_body(
            self.eval_runner,
            "capture_observation_evidence",
        )
        self.assertIn("observation_settle_seconds=%s", evidence)
        self.assertIn("recovery_settle_seconds=%s", evidence)
        self.assertIn(
            'python3 "$SCENARIO_METADATA_VALIDATOR"',
            self.precheck,
        )

        main_loop = self.eval_runner[
            self.eval_runner.find("# ── 메인 루프")
        :]
        recovery_state = main_loop.find('recovery_ok="true"')
        recovery_call = main_loop.find(
            'recover_scenario "$scen"',
            recovery_state,
        )
        recovery_settle = main_loop.find(
            'wait_recovery_settle "$scen"',
            recovery_call,
        )
        cluster_wait = main_loop.find(
            "wait_cluster_stable",
            recovery_settle,
        )
        self.assertGreater(recovery_call, recovery_state)
        self.assertGreater(recovery_settle, recovery_call)
        self.assertGreater(
            cluster_wait,
            recovery_settle,
            "late recovery transitions must be armed before stable polling",
        )

    def test_excluded_scenarios_default_is_empty(self) -> None:
        assignment = re.search(
            r'(?m)^EXCLUDED_SCENARIOS_CSV=(?P<value>.+)$',
            self.eval_runner,
        )
        self.assertIsNotNone(assignment, "EXCLUDED_SCENARIOS_CSV assignment is missing")
        value = assignment.group("value")
        self.assertRegex(
            value,
            r'^"\$\{EXCLUDED_SCENARIOS_CSV(?::-|-)\}"$',
            "all 20 scenarios must run unless exclusions are explicitly supplied",
        )

    def test_eval_timeout_covers_bounded_llm_quota_wait(self) -> None:
        assignment = re.search(
            r'(?m)^EVAL_TIMEOUT_SECONDS='
            r'"\$\{EVAL_TIMEOUT_SECONDS:-([0-9]+)\}"$',
            self.eval_runner,
        )
        self.assertIsNotNone(assignment)
        def env_number(name: str) -> float:
            match = re.search(
                rf"(?m)^{re.escape(name)}=([0-9]+(?:\.[0-9]+)?)$",
                self.env_example,
            )
            self.assertIsNotNone(match, f"{name} must be explicit in env example")
            return float(match.group(1))

        request_timeout = env_number("LLM_REQUEST_TIMEOUT_SECONDS")
        sdk_retries = int(env_number("LLM_MAX_RETRIES"))
        quota_retries = int(env_number("LLM_QUOTA_MAX_RETRIES"))
        quota_wait = env_number("LLM_QUOTA_MAX_WAIT_SECONDS")
        llm_worst_case = (
            quota_wait
            + (quota_retries + 1)
            * (sdk_retries + 1)
            * request_timeout
        )
        query_count_match = re.search(
            r"(?m)^METRICS_CONTEXT_QUERY_COUNT\s*=\s*([0-9]+)$",
            self.metrics_source,
        )
        self.assertIsNotNone(query_count_match)
        prometheus_query_count = int(query_count_match.group(1))
        prometheus_timeout = env_number(
            "PROMETHEUS_REQUEST_TIMEOUT_SECONDS"
        )
        hybrid_worst_case = (
            llm_worst_case
            + prometheus_query_count * prometheus_timeout
        )
        safety_margin = 60
        eval_timeout = int(assignment.group(1))
        self.assertEqual(
            0,
            sdk_retries,
            "SDK retry must not stack with the explicit quota retry",
        )
        self.assertGreaterEqual(
            eval_timeout,
            hybrid_worst_case + safety_margin,
            "curl must cover Prometheus, LLM retries, and an explicit margin",
        )
        self.assertIn(
            '[[ "$EVAL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]',
            self.eval_runner,
        )
        ttl_match = re.search(
            r"(?m)^EVAL_SNAPSHOT_TTL\s*=\s*([0-9]+)\s*\*\s*([0-9]+)$",
            self.backend_main,
        )
        self.assertIsNotNone(ttl_match)
        snapshot_ttl = int(ttl_match.group(1)) * int(ttl_match.group(2))
        snapshot_margin_match = re.search(
            r'(?m)^SNAPSHOT_ACQUIRE_MARGIN_SECONDS='
            r'"\$\{SNAPSHOT_ACQUIRE_MARGIN_SECONDS:-([0-9]+)\}"$',
            self.eval_runner,
        )
        self.assertIsNotNone(snapshot_margin_match)
        snapshot_margin = int(snapshot_margin_match.group(1))
        dfs_planned_match = re.search(
            r'(?m)^DFS_MODE_PLANNED_SECONDS='
            r'"\$\{DFS_MODE_PLANNED_SECONDS:-([0-9]+)\}"$',
            self.eval_runner,
        )
        self.assertIsNotNone(dfs_planned_match)
        dfs_planned = int(dfs_planned_match.group(1))
        llm_interval_match = re.search(
            r'(?m)^LLM_MODE_INTERVAL_SECONDS='
            r'"\$\{LLM_MODE_INTERVAL_SECONDS:-([0-9]+)\}"$',
            self.eval_runner,
        )
        self.assertIsNotNone(llm_interval_match)
        default_llm_interval = int(llm_interval_match.group(1))
        self.assertEqual(
            0,
            default_llm_interval,
            "the static TTL contract assumes the default has no throttle wait",
        )
        self.assertGreaterEqual(
            snapshot_ttl,
            2 * eval_timeout + snapshot_margin,
            "one snapshot must cover both bounded LLM-mode requests",
        )
        self.assertGreaterEqual(
            snapshot_ttl,
            (
                2 * eval_timeout
                + dfs_planned
                + 2 * default_llm_interval
                + snapshot_margin
            ),
            "the snapshot must also cover the conservative DFS budget",
        )

    def test_runner_reserves_client_snapshot_id_before_post(self) -> None:
        body = function_body(self.eval_runner, "call_all_modes")
        generate_id = body.find("secrets.token_hex(16)")
        arm_id = body.find('ACTIVE_SNAPSHOT_ID="$snapshot_id"')
        post_snapshot = body.find(
            '-X POST "$SNAPSHOT_API?snapshot_id=$snapshot_id"'
        )
        request_started = body.find("snapshot_request_started=$SECONDS")
        self.assertGreaterEqual(generate_id, 0)
        self.assertGreater(
            arm_id,
            generate_id,
            "the generated ID must be armed for EXIT cleanup",
        )
        self.assertGreater(
            post_snapshot,
            arm_id,
            "the client ID must be armed before the POST starts",
        )
        self.assertGreater(request_started, arm_id)
        self.assertLess(
            request_started,
            post_snapshot,
            "the conservative TTL clock must start immediately before POST",
        )
        self.assertIn(
            'snapshot_id != expected_snapshot',
            body,
            "the response ID must match the client reservation",
        )

    def test_snapshot_acquisition_failures_release_the_client_reservation(
        self,
    ) -> None:
        body = function_body(self.eval_runner, "call_all_modes")
        post = body.find(
            '-X POST "$SNAPSHOT_API?snapshot_id=$snapshot_id"'
        )
        mode_loop = body.find('for mode in "${MODES[@]}"; do')
        self.assertGreaterEqual(post, 0)
        self.assertGreater(mode_loop, post)
        acquisition = body[post:mode_loop]

        failure_markers = (
            "평가 snapshot 생성 API 오류",
            "평가 snapshot 응답 검증 실패",
            "snapshot 산출물 저장 실패",
            "전체 mode 실행 예산",
        )
        for marker in failure_markers:
            with self.subTest(marker=marker):
                marker_index = acquisition.find(marker)
                self.assertGreaterEqual(marker_index, 0)
                next_return = acquisition.find("return 1", marker_index)
                self.assertGreater(next_return, marker_index)
                self.assertIn(
                    "release_active_snapshot",
                    acquisition[marker_index:next_return],
                )

        release = function_body(self.eval_runner, "release_active_snapshot")
        delete_call = release.find("-X DELETE")
        clear_id = release.find('ACTIVE_SNAPSHOT_ID=""')
        self.assertGreaterEqual(delete_call, 0)
        self.assertGreater(
            clear_id,
            delete_call,
            "failed DELETE must retain the active ID for cleanup retry",
        )

    def test_snapshot_response_deadline_and_guards_precede_waits_and_calls(
        self,
    ) -> None:
        call_modes = function_body(self.eval_runner, "call_all_modes")
        self.assertIn(
            "not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0",
            call_modes,
        )
        deadline = call_modes.find(
            "ACTIVE_SNAPSHOT_DEADLINE="
            "$((snapshot_request_started + snapshot_ttl))"
        )
        all_modes_guard = call_modes.find(
            'guard_snapshot_deadline "$planned_seconds" "전체 mode 실행 예산"'
        )
        mode_loop = call_modes.find('for mode in "${MODES[@]}"; do')
        mode_guard = call_modes.find(
            'guard_snapshot_deadline "$planned_seconds" "$mode 호출"'
        )
        mode_curl = call_modes.find(
            '--max-time "$planned_seconds"',
            mode_loop,
        )
        self.assertGreaterEqual(deadline, 0)
        self.assertGreater(all_modes_guard, deadline)
        self.assertGreater(mode_loop, all_modes_guard)
        self.assertGreater(mode_guard, mode_loop)
        self.assertGreater(
            mode_curl,
            mode_guard,
            "deadline guard must run before each mode curl",
        )
        planned_mode = function_body(
            self.eval_runner,
            "planned_mode_seconds",
        )
        self.assertRegex(
            planned_mode,
            r"hybrid\|llm_only\)\s+printf\s+'%s\\n'\s+"
            r'"\$EVAL_TIMEOUT_SECONDS"',
        )
        self.assertRegex(
            planned_mode,
            r"dfs_only\)\s+printf\s+'%s\\n'\s+"
            r'"\$DFS_MODE_PLANNED_SECONDS"',
        )
        planned_all = function_body(
            self.eval_runner,
            "planned_all_modes_seconds",
        )
        self.assertIn("llm_mode_count", planned_all)
        self.assertIn("snapshot_error_count", planned_all)
        self.assertIn(
            "llm_mode_count * LLM_MODE_INTERVAL_SECONDS",
            planned_all,
        )
        self.assertIn(
            'planned_all_modes_seconds "$snapshot_error_count"',
            call_modes,
        )
        self.assertIn(
            'throttle_llm_mode "$mode" "$snapshot_error_count"',
            call_modes,
        )

        deadline_guard = function_body(
            self.eval_runner,
            "guard_snapshot_deadline",
        )
        self.assertIn(
            "ACTIVE_SNAPSHOT_DEADLINE - SECONDS",
            deadline_guard,
        )
        self.assertIn(
            "required_seconds + SNAPSHOT_ACQUIRE_MARGIN_SECONDS",
            deadline_guard,
        )

        throttle = function_body(self.eval_runner, "throttle_llm_mode")
        throttle_guard = throttle.find("guard_snapshot_deadline")
        throttle_sleep = throttle.find('sleep "$wait_seconds"')
        self.assertGreaterEqual(throttle_guard, 0)
        self.assertGreater(
            throttle_sleep,
            throttle_guard,
            "quota throttle must check the deadline before sleeping",
        )

    def test_planned_all_modes_only_budgets_throttle_for_positive_snapshot(
        self,
    ) -> None:
        planned_mode = re.search(
            r"(?ms)^planned_mode_seconds\(\)\s*\{.*?^\}",
            self.eval_runner,
        )
        planned_all = re.search(
            r"(?ms)^planned_all_modes_seconds\(\)\s*\{.*?^\}",
            self.eval_runner,
        )
        self.assertIsNotNone(planned_mode)
        self.assertIsNotNone(planned_all)
        script = f"""
set -uo pipefail
EVAL_TIMEOUT_SECONDS=10
DFS_MODE_PLANNED_SECONDS=3
LLM_MODE_INTERVAL_SECONDS=7
MODES=(hybrid dfs_only llm_only)
{planned_mode.group(0)}
{planned_all.group(0)}
planned_all_modes_seconds 4
planned_all_modes_seconds 0
"""
        completed = run_bash(script)
        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        self.assertEqual(["37", "23"], completed.stdout.splitlines())

    def test_zero_error_snapshot_skips_llm_pacing_without_advancing_clock(
        self,
    ) -> None:
        throttle = re.search(
            r"(?ms)^throttle_llm_mode\(\)\s*\{.*?^\}",
            self.eval_runner,
        )
        self.assertIsNotNone(throttle)
        call_modes = function_body(self.eval_runner, "call_all_modes")
        mode_loop = call_modes.find('for mode in "${MODES[@]}"; do')
        mode_curl = call_modes.find(
            '--data-urlencode "mode=$mode"',
            mode_loop,
        )
        self.assertGreaterEqual(mode_loop, 0)
        self.assertGreater(
            mode_curl,
            mode_loop,
            "zero-error optimization must retain every real mode request",
        )

        script = f"""
set -uo pipefail
LLM_MODE_INTERVAL_SECONDS=60
LAST_LLM_MODE_EPOCH=90
SLEEP_LOG=""
log() {{ :; }}
date() {{ printf '100\\n'; }}
planned_mode_seconds() {{ printf '10\\n'; }}
guard_snapshot_deadline() {{ return 0; }}
sleep() {{ SLEEP_LOG="${{SLEEP_LOG}}${{1}},"; }}
{throttle.group(0)}
throttle_llm_mode hybrid 0
printf 'clean=%s|%s\\n' "$LAST_LLM_MODE_EPOCH" "$SLEEP_LOG"
throttle_llm_mode hybrid 2
printf 'positive=%s|%s\\n' "$LAST_LLM_MODE_EPOCH" "$SLEEP_LOG"
"""
        completed = run_bash(script)
        self.assertEqual(
            0,
            completed.returncode,
            completed.stdout + completed.stderr,
        )
        self.assertEqual(
            ["clean=90|", "positive=100|50,"],
            completed.stdout.splitlines(),
        )

    def test_zero_error_mode_outputs_are_fail_closed(self) -> None:
        call_modes = function_body(self.eval_runner, "call_all_modes")
        self.assertIn(
            'if errors == 0 and (chains != 0 or chain_items):',
            call_modes,
        )
        self.assertIn(
            'data.get("ai_status") != "not_applicable"',
            call_modes,
        )
        self.assertIn(
            'if ai_text: raise ValueError("zero-error response contains LLM analysis")',
            call_modes,
        )

    def test_excluded_scenarios_are_skipped_before_injection(self) -> None:
        injection = self.eval_runner.find('inject_scenario "$scen"')
        exclusion = self.eval_runner.find(
            'if is_in_list "$scen" "${EXCLUDED_SCENARIOS[@]}"; then'
        )
        self.assertGreaterEqual(injection, 0)
        self.assertGreaterEqual(exclusion, 0)
        self.assertLess(
            exclusion,
            injection,
            "excluded scenarios must never be fault-injected",
        )
        between = self.eval_runner[exclusion:injection]
        self.assertIn(
            'record_status "$scen" "excluded"',
            between,
        )
        self.assertIn("continue", between)

    def test_active_scenario_is_armed_before_injection(self) -> None:
        self.assertRegex(
            self.eval_runner,
            r'(?m)^ACTIVE_SCENARIO=""$',
            "ACTIVE_SCENARIO must have a safe empty initial value",
        )
        injection = self.eval_runner.find('inject_scenario "$scen"')
        self.assertGreaterEqual(injection, 0, "main scenario injection call is missing")
        prefix = self.eval_runner[max(0, injection - 400) : injection]
        self.assertRegex(
            prefix,
            r'(?m)^\s*ACTIVE_SCENARIO="\$scen"\s*$',
            "the current scenario must be armed before its fault is injected",
        )

    def test_cluster_stability_polls_current_structured_state(self) -> None:
        body = function_body(self.eval_runner, "wait_cluster_stable")
        hard_timeout = function_body(
            self.eval_runner,
            "cluster_kubectl_with_hard_timeout",
        )
        self.assertNotRegex(body, r"(?m)^\s*kubectl\s+wait\b")
        self.assertIn("timeout --signal=KILL", hard_timeout)
        self.assertIn("CLUSTER_STABILITY_TIMEOUT_SECONDS", body)
        self.assertIn("CLUSTER_STABILITY_POLL_INTERVAL_SECONDS", body)
        self.assertIn("CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS", body)
        self.assertIn("CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV", body)
        self.assertIn("get deployment,pod", body)
        self.assertIn("deletionTimestamp", body)
        for field in (
            "generation",
            "observedGeneration",
            "replicas",
            "updatedReplicas",
            "readyReplicas",
            "availableReplicas",
            "unavailableReplicas",
            "containerStatuses",
        ):
            self.assertIn(field, body)
        self.assertIn('phase = status.get("phase"', body)
        self.assertIn('condition.get("type") == "Ready"', body)
        self.assertIn("current_pods=0", body)
        self.assertIn("missing_deployments=", body)

    def test_cluster_stability_ignores_deleted_old_pod_after_replacement(
        self,
    ) -> None:
        hard_timeout = (
            "cluster_kubectl_with_hard_timeout() {\n"
            + function_body(
                self.eval_runner,
                "cluster_kubectl_with_hard_timeout",
            )
            + "}\n"
        )
        wait_stable = (
            "wait_cluster_stable() {\n"
            + function_body(self.eval_runner, "wait_cluster_stable")
            + "}\n"
        )
        first_payload = shlex.quote(
            kubernetes_list(
                deployment_item("catalogue", converged=False),
                pod_item("catalogue-old", ready=False),
            )
        )
        second_payload = shlex.quote(
            kubernetes_list(
                deployment_item("catalogue"),
                pod_item("catalogue-old", ready=False, terminating=True),
                pod_item("catalogue-new"),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = shlex.quote(
                bash_path(Path(temp_dir) / "kubectl-calls")
            )
            script = f"""
set -uo pipefail
log() {{ printf '%s\\n' "$*"; }}
{hard_timeout}
{wait_stable}
mock_state={state_path}
first_payload={first_payload}
second_payload={second_payload}
export mock_state first_payload second_payload
kubectl() {{
  local calls=0
  if [ -f "$mock_state" ]; then
    calls="$(cat "$mock_state")"
  fi
  calls=$((calls + 1))
  printf '%s' "$calls" > "$mock_state"
  if [ "$calls" -eq 1 ]; then
    printf '%s\\n' "$first_payload"
  else
    printf '%s\\n' "$second_payload"
  fi
}}
NS=sock-shop
CLUSTER_STABILITY_TIMEOUT_SECONDS=5
CLUSTER_STABILITY_POLL_INTERVAL_SECONDS=1
CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV=catalogue
wait_cluster_stable
printf 'calls=%s\\n' "$(cat "$mock_state")"
"""
            completed = run_bash(script)

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("calls=2", completed.stdout)
        self.assertIn("current_pods=1", completed.stdout)

    def test_cluster_stability_rejects_missing_expected_deployment(self) -> None:
        hard_timeout = (
            "cluster_kubectl_with_hard_timeout() {\n"
            + function_body(
                self.eval_runner,
                "cluster_kubectl_with_hard_timeout",
            )
            + "}\n"
        )
        wait_stable = (
            "wait_cluster_stable() {\n"
            + function_body(self.eval_runner, "wait_cluster_stable")
            + "}\n"
        )
        mock_payload = shlex.quote(
            kubernetes_list(
                deployment_item("catalogue"),
                pod_item("catalogue-current"),
            )
        )
        script = f"""
set -uo pipefail
log() {{ printf '%s\\n' "$*"; }}
{hard_timeout}
{wait_stable}
mock_payload={mock_payload}
export mock_payload
kubectl() {{ printf '%s\\n' "$mock_payload"; }}
NS=sock-shop
CLUSTER_STABILITY_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_POLL_INTERVAL_SECONDS=1
CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV=catalogue,orders
wait_cluster_stable
"""
        completed = run_bash(script)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("missing_deployments=orders", completed.stdout)

    def test_cluster_stability_rejects_empty_workload_list(self) -> None:
        hard_timeout = (
            "cluster_kubectl_with_hard_timeout() {\n"
            + function_body(
                self.eval_runner,
                "cluster_kubectl_with_hard_timeout",
            )
            + "}\n"
        )
        wait_stable = (
            "wait_cluster_stable() {\n"
            + function_body(self.eval_runner, "wait_cluster_stable")
            + "}\n"
        )
        mock_payload = shlex.quote(kubernetes_list())
        script = f"""
set -uo pipefail
log() {{ printf '%s\\n' "$*"; }}
{hard_timeout}
{wait_stable}
mock_payload={mock_payload}
export mock_payload
kubectl() {{ printf '%s\\n' "$mock_payload"; }}
NS=sock-shop
CLUSTER_STABILITY_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_POLL_INTERVAL_SECONDS=1
CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV=catalogue
wait_cluster_stable
"""
        completed = run_bash(script)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("deployments=0", completed.stdout)
        self.assertIn("current_pods=0", completed.stdout)

    def test_cluster_stability_bounds_a_stalled_kubectl_request(self) -> None:
        hard_timeout = (
            "cluster_kubectl_with_hard_timeout() {\n"
            + function_body(
                self.eval_runner,
                "cluster_kubectl_with_hard_timeout",
            )
            + "}\n"
        )
        wait_stable = (
            "wait_cluster_stable() {\n"
            + function_body(self.eval_runner, "wait_cluster_stable")
            + "}\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            child_pid_path = shlex.quote(
                bash_path(Path(temp_dir) / "kubectl-child.pid")
            )
            script = f"""
set -uo pipefail
log() {{ printf '%s\\n' "$*"; }}
{hard_timeout}
{wait_stable}
mock_child_pid_file={child_pid_path}
export mock_child_pid_file
kubectl() {{
  trap "" TERM
  (trap "" TERM; sleep 5) &
  printf '%s\\n' "$!" > "$mock_child_pid_file"
  wait
}}
NS=sock-shop
CLUSTER_STABILITY_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_POLL_INTERVAL_SECONDS=1
CLUSTER_STABILITY_REQUEST_TIMEOUT_SECONDS=1
CLUSTER_STABILITY_EXPECTED_DEPLOYMENTS_CSV=catalogue
started_ns="$(date +%s%N)"
wait_cluster_stable
rc=$?
finished_ns="$(date +%s%N)"
elapsed_ms=$(((finished_ns - started_ns) / 1000000))
child_state=gone
if [ -s "$mock_child_pid_file" ]; then
  child_pid="$(cat "$mock_child_pid_file")"
  if kill -0 "$child_pid" 2>/dev/null; then
    child_state="$(awk '{{print $3}}' "/proc/$child_pid/stat")"
  fi
fi
printf 'helper_elapsed_ms=%s\\nchild_state=%s\\n' "$elapsed_ms" "$child_state"
case "$child_state" in
  gone|Z) ;;
  *) exit 90 ;;
esac
exit "$rc"
"""
            completed = run_bash(script)

        self.assertEqual(completed.returncode, 1)
        elapsed_match = re.search(
            r"(?m)^helper_elapsed_ms=([0-9]+)$",
            completed.stdout,
        )
        self.assertIsNotNone(elapsed_match, completed.stdout + completed.stderr)
        self.assertLess(
            int(elapsed_match.group(1)),
            3000,
            "a stalled kubectl request must not outlive the stability deadline",
        )
        self.assertRegex(completed.stdout, r"(?m)^child_state=(?:gone|Z)$")
        self.assertIn("kubectl 현재 상태 조회 실패/시간 초과", completed.stdout)

    def test_active_scenario_is_cleared_after_successful_recovery(self) -> None:
        conditional_recovery = re.search(
            r'(?ms)if\s+recover_scenario\s+"\$scen"\s*;\s*then'
            r'(?:(?!^\s*(?:else|fi)\b).)*?ACTIVE_SCENARIO=""',
            self.eval_runner,
        )
        short_circuit_recovery = re.search(
            r'recover_scenario\s+"\$scen"\s*&&\s*ACTIVE_SCENARIO=""',
            self.eval_runner,
        )
        guarded_recovery = False
        recovery_marker = re.search(
            r'(?m)^\s*recovery_ok="true"\s*$',
            self.eval_runner,
        )
        if recovery_marker is not None:
            recovery_call = self.eval_runner.find(
                'recover_scenario "$scen"',
                recovery_marker.end(),
            )
            failure_guard = self.eval_runner.find(
                'if [ "$recovery_ok" != "true" ]',
                recovery_call,
            )
            clear = self.eval_runner.find(
                'ACTIVE_SCENARIO=""',
                failure_guard,
            )
            guarded_recovery = (
                recovery_call >= 0
                and failure_guard > recovery_call
                and clear > failure_guard
            )
        self.assertTrue(
            conditional_recovery or short_circuit_recovery or guarded_recovery,
            "ACTIVE_SCENARIO must be cleared only on a successful normal recovery",
        )

    def test_wait_rollout_polls_actual_deployment_convergence(self) -> None:
        body = function_body(self.scenario_lib, "wait_rollout")
        hard_timeout = function_body(
            self.scenario_lib,
            "kubectl_with_hard_timeout",
        )
        self.assertNotRegex(
            body,
            r"(?m)^\s*kubectl\s+rollout\s+status\b",
            "a stale ProgressDeadlineExceeded condition must not abort recovery",
        )
        self.assertIn("timeout --signal=KILL", hard_timeout)
        for field in (
            ".metadata.generation",
            ".status.observedGeneration",
            ".spec.replicas",
            ".status.replicas",
            ".status.updatedReplicas",
            ".status.readyReplicas",
            ".status.availableReplicas",
            ".status.unavailableReplicas",
        ):
            self.assertIn(field, body)
        self.assertIn('timeout_value="${ROLLOUT_TIMEOUT:-300s}"', body)
        self.assertRegex(body, r'\[ "\$observed" -ge "\$generation" \]')
        for status_field in ("replicas", "updated", "ready", "available"):
            self.assertRegex(
                body,
                rf'\[ "\${status_field}" -eq "\$desired" \]',
            )
        self.assertRegex(body, r'\[ "\$unavailable" -eq 0 \]')
        self.assertIn(
            '[[ "$poll_interval" =~ ^[1-9][0-9]*$ ]]',
            body,
        )

    def test_wait_rollout_ignores_stale_progress_condition_in_mock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "kubectl-calls"
            source_path = shlex.quote(bash_path(SCENARIO_LIB_PATH))
            mock_state = shlex.quote(bash_path(state_path))
            script = f"""
source {source_path}
mock_state={mock_state}
export mock_state
kubectl() {{
  if [[ "$*" == *"rollout status"* ]]; then
    return 90
  fi
  [[ "$*" == *"get deployment catalogue"* ]] || return 91
  local calls=0
  if [ -f "$mock_state" ]; then
    calls="$(cat "$mock_state")"
  fi
  calls=$((calls + 1))
  printf '%s' "$calls" > "$mock_state"
  if [ "$calls" -eq 1 ]; then
    printf '9|9|1|1|1|0|0|1\\n'
  else
    printf '9|9|1|1|1|1|1|0\\n'
  fi
}}
ROLLOUT_TIMEOUT=6s
ROLLOUT_POLL_INTERVAL_SECONDS=1
wait_rollout catalogue
printf 'calls=%s\\n' "$(cat "$mock_state")"
"""
            completed = run_bash(script)
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertIn("calls=2", completed.stdout)
            self.assertIn("rollout converged", completed.stdout)

    def test_wait_rollout_accepts_converged_zero_replicas(self) -> None:
        source_path = shlex.quote(bash_path(SCENARIO_LIB_PATH))
        script = f"""
source {source_path}
kubectl() {{
  printf '9|10|0|||||\\n'
}}
ROLLOUT_TIMEOUT=1s
ROLLOUT_POLL_INTERVAL_SECONDS=1
wait_rollout catalogue
"""
        completed = run_bash(script)
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("replicas=0", completed.stdout)

    def test_wait_rollout_honors_global_timeout(self) -> None:
        source_path = shlex.quote(bash_path(SCENARIO_LIB_PATH))
        script = f"""
source {source_path}
kubectl() {{
  printf '9|9|1|1|1|0|0|1\\n'
}}
ROLLOUT_TIMEOUT=1s
ROLLOUT_POLL_INTERVAL_SECONDS=1
wait_rollout catalogue
"""
        completed = run_bash(script)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("rollout convergence timeout", completed.stdout)

    def test_wait_rollout_bounds_a_stalled_kubectl_request(self) -> None:
        source_path = shlex.quote(bash_path(SCENARIO_LIB_PATH))
        script = f"""
source {source_path}
kubectl() {{
  sleep 5
  printf '9|9|1|1|1|1|1|0\\n'
}}
ROLLOUT_TIMEOUT=1s
ROLLOUT_POLL_INTERVAL_SECONDS=1
ROLLOUT_REQUEST_TIMEOUT_SECONDS=1
started_ns="$(date +%s%N)"
set +e
wait_rollout catalogue
rc=$?
set -e
finished_ns="$(date +%s%N)"
printf 'helper_elapsed_ms=%s\\n' \
  "$(((finished_ns - started_ns) / 1000000))"
exit "$rc"
"""
        completed = run_bash(script)

        self.assertEqual(completed.returncode, 1)
        elapsed_match = re.search(
            r"(?m)^helper_elapsed_ms=([0-9]+)$",
            completed.stdout,
        )
        self.assertIsNotNone(elapsed_match, completed.stdout + completed.stderr)
        self.assertLess(
            int(elapsed_match.group(1)),
            3000,
            "a stalled kubectl request must not outlive the rollout deadline",
        )
        self.assertIn("rollout convergence timeout", completed.stdout)

    def test_wait_rollout_rejects_zero_poll_interval(self) -> None:
        source_path = shlex.quote(bash_path(SCENARIO_LIB_PATH))
        script = f"""
source {source_path}
ROLLOUT_TIMEOUT=1s
ROLLOUT_POLL_INTERVAL_SECONDS=0
wait_rollout catalogue
"""
        completed = run_bash(script)
        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            "invalid ROLLOUT_POLL_INTERVAL_SECONDS: 0",
            completed.stderr,
        )

    def test_recovery_log_resets_once_then_appends_attempts(self) -> None:
        recover_definition = (
            "recover_scenario() {\n"
            + function_body(self.eval_runner, "recover_scenario")
            + "}\n"
        )
        self.assertIn(
            'pipeline_status=("${PIPESTATUS[@]}")',
            recover_definition,
        )
        self.assertIn(
            '${pipeline_status[1]}',
            recover_definition,
            "a tee failure must make recovery fail instead of losing evidence",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scenario_root = root / "scenarios"
            result_root = root / "results"
            scenario = "07-liveness-probe"
            scenario_dir = scenario_root / scenario
            result_dir = result_root / scenario
            scenario_dir.mkdir(parents=True)
            result_dir.mkdir(parents=True)
            (scenario_dir / "recover.sh").write_text(
                "#!/usr/bin/env bash\nprintf 'recover-body\\n'\n",
                encoding="utf-8",
                newline="\n",
            )
            recover_log = result_dir / "recover.log"
            recover_log.write_text("stale-prior-log\n", encoding="utf-8")

            script = f"""
set -uo pipefail
SCENARIO_DIR={shlex.quote(bash_path(scenario_root))}
RESULT_DIR={shlex.quote(bash_path(result_root))}
declare -A RECOVERY_ATTEMPTS=()
log() {{ :; }}
{recover_definition}
recover_scenario {shlex.quote(scenario)}
recover_scenario {shlex.quote(scenario)}
"""
            completed = run_bash(script)
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            content = recover_log.read_text(encoding="utf-8")
            self.assertNotIn("stale-prior-log", content)
            self.assertEqual(content.count("recover attempt=1"), 1)
            self.assertEqual(content.count("recover attempt=2"), 1)
            self.assertEqual(content.count("recover-body"), 2)
            self.assertLess(
                content.find("recover attempt=1"),
                content.find("recover attempt=2"),
            )

    def test_exit_trap_recovers_active_scenario_and_releases_snapshot(self) -> None:
        cleanup = function_body(self.eval_runner, "cleanup_on_exit")
        recovery_text = cleanup
        if "recover_active_scenario" in cleanup:
            recovery_text += function_body(self.eval_runner, "recover_active_scenario")

        self.assertIn(
            "ACTIVE_SCENARIO",
            recovery_text,
            "EXIT cleanup must inspect whether a scenario is still active",
        )
        self.assertRegex(
            recovery_text,
            r'recover_scenario\s+"\$\{?ACTIVE_SCENARIO\}?"',
            "EXIT cleanup must recover the active scenario",
        )
        self.assertIn(
            "release_active_snapshot",
            cleanup,
            "EXIT cleanup must release an active evaluation snapshot",
        )
        self.assertRegex(
            self.eval_runner,
            r"(?m)^trap\s+cleanup_on_exit\s+EXIT$",
            "cleanup_on_exit must be registered for EXIT",
        )

    def test_fault_manifest_is_deleted_before_apply(self) -> None:
        inject = function_body(self.eval_runner, "inject_scenario")
        delete_fault = inject.find('kubectl delete -f "$dir/fault.yaml"')
        apply_fault = inject.find('kubectl apply -f "$dir/fault.yaml"')
        self.assertGreaterEqual(
            delete_fault,
            0,
            "a fixed-name Chaos manifest must be deleted before it is re-applied",
        )
        self.assertGreater(
            apply_fault,
            delete_fault,
            "fault.yaml deletion must precede fault.yaml apply",
        )
        self.assertIn(
            "--ignore-not-found",
            inject[delete_fault:apply_fault],
            "pre-injection deletion must be idempotent",
        )

    def test_pod_failure_uses_real_podchaos_not_scale_to_zero(self) -> None:
        self.assertFalse(
            (POD_FAILURE_DIR / "inject.sh").exists(),
            "an active inject.sh would override the PodChaos manifest",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?m)^kind:\s*PodChaos\s*$",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?ms)^metadata:\s*\n"
            r"\s+name:\s*kubein-02-pod-failure\s*\n"
            r"\s+namespace:\s*sock-shop\s*$",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?m)^\s*action:\s*pod-failure\s*$",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?m)^\s*mode:\s*one\s*$",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?m)^\s*duration:\s*120s\s*$",
        )
        self.assertRegex(
            self.pod_failure_fault,
            r"(?ms)labelSelectors:\s*\n\s+name:\s*catalogue\s*$",
        )
        self.assertIn("--replicas=0", self.pod_failure_legacy_inject)
        self.assertIn("restore_replicas catalogue", self.pod_failure_legacy_recover)
        delete_fault = self.pod_failure_recover.find("kubectl delete -f")
        delete_pod = self.pod_failure_recover.find("kubectl delete pod")
        wait_rollout = self.pod_failure_recover.find("wait_rollout catalogue")
        self.assertGreaterEqual(delete_fault, 0)
        self.assertGreater(
            delete_pod,
            delete_fault,
            "Pod must be replaced only after Chaos Mesh restores its image",
        )
        self.assertGreater(wait_rollout, delete_pod)
        self.assertIn("--ignore-not-found", self.pod_failure_recover)
        self.assertIn("-l name=catalogue", self.pod_failure_recover)

        recover = function_body(self.eval_runner, "recover_scenario")
        fault_delete = recover.find('kubectl delete -f "$dir/fault.yaml"')
        self.assertGreaterEqual(
            fault_delete,
            0,
            "manifest-backed scenarios must delete their fault during recovery",
        )
        self.assertIn(
            "--ignore-not-found",
            recover[fault_delete:],
            "fault recovery must remain idempotent",
        )

    def test_dns_chaos_prepares_and_restores_writable_rootfs(self) -> None:
        inject = function_body(self.eval_runner, "inject_scenario")
        prepare_hook = inject.find('"$dir/prepare.sh"')
        apply_fault = inject.find('kubectl apply -f "$dir/fault.yaml"')
        self.assertGreaterEqual(prepare_hook, 0, "runner must support scenario preparation")
        self.assertGreater(
            apply_fault,
            prepare_hook,
            "scenario preparation must run before Chaos fault application",
        )
        self.assertIn("set_root_filesystem_writable", self.scenario_lib)
        self.assertIn("set_root_filesystem_writable shipping", self.dns_prepare)
        self.assertIn("kubein-dnschaos-target", self.scenario_lib)
        self.assertIn("kubein-dnschaos-target", self.dns_fault)
        delete_fault = self.dns_recover.find("kubectl delete -f")
        restore_template = self.dns_recover.find(
            "restore_deployment_template shipping"
        )
        self.assertGreaterEqual(delete_fault, 0)
        self.assertGreater(
            restore_template,
            delete_fault,
            "DNSChaos must be removed before restoring the shipping template",
        )
        self.assertIn(
            "SHIPPING_READ_ONLY_ROOTFS",
            self.precheck,
            "precheck must reject a leaked writable shipping root filesystem",
        )
        self.assertIn("kubein-dnschaos-target", self.dns_recover)
        self.assertIn("DNS_TARGET_RESIDUE", self.precheck)

    def test_io_chaos_prepares_and_restores_writable_rootfs(self) -> None:
        self.assertIn("set_root_filesystem_writable_for_target", self.scenario_lib)
        self.assertIn(
            "set_root_filesystem_writable_for_target carts-db kubein-iochaos-target",
            self.io_prepare,
        )
        self.assertIn("kubein-iochaos-target", self.io_fault)
        delete_fault = self.io_recover.find("kubectl delete -f")
        restore_template = self.io_recover.find(
            "restore_deployment_template carts-db"
        )
        self.assertGreaterEqual(delete_fault, 0)
        self.assertGreater(
            restore_template,
            delete_fault,
            "IOChaos must be removed before restoring the carts-db template",
        )
        self.assertIn("injectedCount", self.io_recover)
        self.assertIn('conditions.get("AllInjected") != "True"', self.io_recover)
        self.assertIn("CARTS_DB_READ_ONLY_ROOTFS", self.precheck)
        self.assertIn("IO_TARGET_RESIDUE", self.precheck)

    def test_observation_evidence_and_diagnosis_hooks_are_called(self) -> None:
        function_body(self.eval_runner, "capture_observation_evidence")
        function_body(self.eval_runner, "diagnose_observation_failure")

        injection = self.eval_runner.find('inject_scenario "$scen"')
        self.assertGreaterEqual(injection, 0)
        main_loop_tail = self.eval_runner[injection:]
        self.assertRegex(
            main_loop_tail,
            r"(?m)^\s*(?:if\s+!?\s*)?capture_observation_evidence\b(?!\s*\(\))",
            "each injected scenario must capture observation evidence",
        )
        self.assertRegex(
            self.eval_runner,
            r"(?m)^\s*(?:if\s+!?\s*)?diagnose_observation_failure\b(?!\s*\(\))",
            "observation failures must invoke the diagnosis recorder",
        )
        diagnosis = function_body(self.eval_runner, "diagnose_observation_failure")
        injection_failure = diagnosis.find('classification = "fault_injection_failure"')
        expected_gap = diagnosis.find('classification = "ground_truth_undetectable"')
        self.assertGreaterEqual(injection_failure, 0)
        self.assertGreater(
            expected_gap,
            injection_failure,
            "an actual injection failure must not be mislabeled as GT-undetectable",
        )
        self.assertIn('load_json("fault-status.json")', diagnosis)
        self.assertIn(
            "if failed_fault_status is not None",
            diagnosis,
            "successful fault status must enrich diagnosis without being classified as injection failure",
        )

    def test_precheck_fails_when_chaos_residue_exists(self) -> None:
        lowered = self.precheck.lower()
        for resource in (
            "podchaos",
            "dnschaos",
            "networkchaos",
            "stresschaos",
            "iochaos",
            "podiochaos",
        ):
            self.assertIn(
                resource,
                lowered,
                f"precheck residue scan must include {resource}",
            )

        residue = re.search(r"(?m)^.*CHAOS_RESIDUE.*$", self.precheck)
        self.assertIsNotNone(residue, "precheck must collect Chaos residue")
        residue_branch = self.precheck[residue.start() : residue.start() + 1600]
        self.assertRegex(
            residue_branch,
            r'(?m)^\s*fail\s+".*(?:잔류|residue)',
            "non-empty Chaos residue must be a precheck failure",
        )

    def test_five_cycle_runner_calls_three_mode_integrity_validator(self) -> None:
        validator = function_body(self.five_cycle, "validate_three_mode_integrity")
        self.assertTrue(
            re.search(r"\bpython3\b", validator)
            or re.search(r"\b[A-Z][A-Z0-9_]*VALIDATOR\b", validator),
            "the integrity hook must delegate to an external validator",
        )

        call = re.search(
            r'(?m)^\s*(?:if\s+!\s*)?validate_three_mode_integrity\s+"\$LATEST_DIR"',
            self.five_cycle,
        )
        self.assertIsNotNone(
            call,
            "each completed cycle must invoke the three-mode integrity validator",
        )
        results_append = self.five_cycle.find('RESULTS+=("$LATEST_DIR")')
        self.assertGreater(
            results_append,
            call.start(),
            "mode integrity must be verified before accepting the cycle result",
        )


if __name__ == "__main__":
    unittest.main()
