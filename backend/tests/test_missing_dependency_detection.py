"""Regression tests for missing ConfigMap/Secret/PVC RCA promotion."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from analysis.graph import DependencyGraph
from infra.models import MiniPod


def make_cache(pod: MiniPod) -> SimpleNamespace:
    """Return the smallest ResourceCache-shaped object needed by DependencyGraph."""
    return SimpleNamespace(
        nodes=[],
        pods=[pod],
        deployments=[],
        replica_sets=[],
        daemon_sets=[],
        stateful_sets=[],
        services=[],
        ingresses=[],
        pvcs=[],
        configmaps=[],
        secrets=[],
        storage_classes=set(),
        hpas=[],
        jobs=[],
        cronjobs=[],
        pvs=[],
        cm_exists=set(),
        secret_exists=set(),
    )


def make_pod(*, phase: str, status: str, reason: str = "") -> MiniPod:
    return MiniPod(
        name="example",
        namespace="default",
        phase=phase,
        node_name="",
        owner=None,
        containers=[],
        pvc_names=[],
        cm_names=["missing-config"],
        secret_names=[],
        status=status,
        error_reason=reason,
    )


class MissingDependencyDetectionTests(unittest.TestCase):
    def test_running_pod_missing_configmap_is_not_a_root_cause(self) -> None:
        graph = DependencyGraph(make_cache(make_pod(phase="Running", status="OK")))

        self.assertNotIn("ConfigMap/default/missing-config", graph.nodes)
        self.assertEqual([], graph.find_root_causes_dfs_only())

    def test_terminal_pod_missing_configmap_is_not_promoted(self) -> None:
        graph = DependencyGraph(
            make_cache(make_pod(phase="Succeeded", status="OK"))
        )

        self.assertNotIn("ConfigMap/default/missing-config", graph.nodes)
        self.assertEqual([], graph.find_root_causes_dfs_only())

    def test_startup_config_error_promotes_missing_configmap(self) -> None:
        graph = DependencyGraph(
            make_cache(
                make_pod(
                    phase="Pending",
                    status="ERROR",
                    reason="CreateContainerConfigError",
                )
            )
        )

        missing = graph.nodes["ConfigMap/default/missing-config"]
        self.assertEqual("ERROR", missing.status)
        self.assertEqual("Missing", missing.error_reason)

        results = graph.find_root_causes_dfs_only()
        self.assertTrue(
            any(result.root_cause_kind == "ConfigMap" for result in results)
        )


if __name__ == "__main__":
    unittest.main()
