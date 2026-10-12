"""Regression contracts for the opt-in network observability extension."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_GT_PATH = REPO_ROOT / "eval" / "ground_truth.json"
EXTENDED_GT_PATH = (
    REPO_ROOT / "eval" / "ground_truth.network-extended.json"
)
PROBE_DIR = REPO_ROOT / "cluster-port" / "network-probe"


class NetworkGroundTruthProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = json.loads(
            BASELINE_GT_PATH.read_text(encoding="utf-8")
        )
        cls.extended = json.loads(
            EXTENDED_GT_PATH.read_text(encoding="utf-8")
        )

    def test_extended_profile_changes_only_network_scenarios(self) -> None:
        changed = {
            scenario
            for scenario in self.baseline
            if self.baseline[scenario] != self.extended[scenario]
        }

        self.assertEqual(set(self.baseline), set(self.extended))
        self.assertEqual(
            {"11-dns-error", "12-network-delay", "13-network-loss"},
            changed,
        )
        self.assertEqual(
            3,
            sum(
                entry["detectable"] for entry in self.extended.values()
            )
            - sum(
                entry["detectable"] for entry in self.baseline.values()
            ),
        )

    def test_extended_network_root_cause_tuples_are_exact(self) -> None:
        expected = {
            "11-dns-error": (
                "NetworkPath",
                "shipping->catalogue[dns]",
                "DNSProbeFailed",
            ),
            "12-network-delay": (
                "NetworkPath",
                "payment->catalogue[http]",
                "HighNetworkLatency",
            ),
            "13-network-loss": (
                "NetworkPath",
                "front-end->front-end[http]",
                "HTTPProbeFailed",
            ),
        }

        for scenario, root_cause in expected.items():
            with self.subTest(scenario=scenario):
                entry = self.extended[scenario]
                self.assertTrue(entry["detectable"])
                self.assertEqual(
                    root_cause,
                    (
                        entry["expected_rc_kind"],
                        entry["expected_rc_name_contains"],
                        entry["expected_rc_reason"],
                    ),
                )


class NetworkProbeDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.install = (PROBE_DIR / "install.sh").read_text(
            encoding="utf-8"
        )
        cls.uninstall = (PROBE_DIR / "uninstall.sh").read_text(
            encoding="utf-8"
        )
        cls.podmonitor = (
            PROBE_DIR / "deploy" / "podmonitor.yaml"
        ).read_text(encoding="utf-8")
        cls.dockerfile = (PROBE_DIR / "Dockerfile").read_text(
            encoding="utf-8"
        )
        cls.exporter = (PROBE_DIR / "app.py").read_text(
            encoding="utf-8"
        )

    def test_sidecar_is_bounded_and_does_not_change_pod_readiness(self) -> None:
        self.assertNotIn("readinessProbe:", self.install)
        self.assertIn("readOnlyRootFilesystem: true", self.install)
        self.assertIn("allowPrivilegeEscalation: false", self.install)
        self.assertIn("cpu: 5m", self.install)
        self.assertIn("memory: 24Mi", self.install)
        self.assertIn("cpu: 100m", self.install)
        self.assertIn("memory: 64Mi", self.install)
        self.assertIn("imagePullPolicy: Always", self.install)

    def test_probe_identity_labels_are_added_and_removed(self) -> None:
        for label in (
            "kubein.io/network-probe",
            "kubein.io/network-probe-type",
            "kubein.io/network-probe-target",
        ):
            with self.subTest(label=label):
                self.assertIn(label, self.install)
                self.assertIn(label, self.uninstall)

        self.assertIn(
            'PROBE_IMAGE_PULL_SECRET="${PROBE_IMAGE_PULL_SECRET:-}"',
            self.install,
        )
        self.assertIn(
            'kubectl get secret "$PROBE_IMAGE_PULL_SECRET"',
            self.install,
        )
        self.assertIn(
            'ROLLOUT_TIMEOUT_SECONDS="${PROBE_ROLLOUT_TIMEOUT_SECONDS:-360}"',
            self.install,
        )
        self.assertIn(
            '--timeout="${ROLLOUT_TIMEOUT_SECONDS}s"',
            self.install,
        )
        self.assertIn("$patch: delete", self.uninstall)

        for mapping in (
            '"dns" \\\n  "catalogue"',
            '"http" \\\n  "catalogue"',
            '"http" \\\n  "front-end"',
        ):
            with self.subTest(mapping=mapping):
                self.assertIn(mapping, self.install)

    def test_podmonitor_exports_complete_probe_identity(self) -> None:
        self.assertIn("- port: netprobe", self.podmonitor)
        self.assertIn("- name: netprobe", self.install)
        self.assertLessEqual(len("netprobe"), 15)
        self.assertIn("interval: 3s", self.podmonitor)
        self.assertIn("scrapeTimeout: 2s", self.podmonitor)
        for target_label in (
            "source_pod",
            "source_workload",
            "probe",
            "target",
        ):
            with self.subTest(target_label=target_label):
                self.assertIn(
                    f"targetLabel: {target_label}",
                    self.podmonitor,
                )

    def test_exporter_exposes_probe_observation_wall_time(self) -> None:
        self.assertIn(
            "kubein_network_probe_observed_timestamp_seconds",
            self.exporter,
        )
        self.assertIn("observed_at", self.exporter)

    def test_probe_base_image_is_digest_pinned(self) -> None:
        first_line = self.dockerfile.splitlines()[0]
        self.assertRegex(
            first_line,
            r"^FROM python:3\.11-slim@sha256:[0-9a-f]{64}$",
        )


if __name__ == "__main__":
    unittest.main()
