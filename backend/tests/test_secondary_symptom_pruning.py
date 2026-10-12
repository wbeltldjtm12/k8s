"""Regression tests for causal pruning of missing-dependency symptoms."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from analysis.graph import DependencyGraph
from infra.models import (
    MiniDeployment,
    MiniPod,
    MiniReplicaSet,
    MiniService,
)


def make_pod(
    name: str,
    *,
    owner: tuple[str, str] | None = None,
    status: str = "OK",
    reason: str = "",
    phase: str = "Running",
    labels: dict[str, str] | None = None,
    secret_names: list[str] | None = None,
    pvc_names: list[str] | None = None,
) -> MiniPod:
    return MiniPod(
        name=name,
        namespace="sock-shop",
        phase=phase,
        node_name="",
        owner=owner,
        containers=[],
        pvc_names=list(pvc_names or []),
        cm_names=[],
        secret_names=list(secret_names or []),
        labels=dict(labels or {}),
        status=status,
        error_reason=reason,
    )


def make_cache(
    *,
    pods: list[MiniPod],
    deployments: list[MiniDeployment] | None = None,
    replica_sets: list[MiniReplicaSet] | None = None,
    services: list[MiniService] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        nodes=[],
        pods=pods,
        deployments=list(deployments or []),
        replica_sets=list(replica_sets or []),
        daemon_sets=[],
        stateful_sets=[],
        services=list(services or []),
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


def aggregate_topology(
    pod: MiniPod,
    *,
    service_name: str = "catalogue",
) -> SimpleNamespace:
    return make_cache(
        pods=[pod],
        replica_sets=[
            MiniReplicaSet(
                name="catalogue-rs",
                namespace="sock-shop",
                desired=1,
                ready=0,
                owner_name="catalogue",
            )
        ],
        deployments=[
            MiniDeployment(
                name="catalogue",
                namespace="sock-shop",
                desired=1,
                available=0,
                ready=0,
                status="ERROR",
                error_reason="NoAvailableReplicas",
            )
        ],
        services=[
            MiniService(
                name=service_name,
                namespace="sock-shop",
                selector={"name": "catalogue"},
                cluster_ip="10.0.0.10",
                svc_type="ClusterIP",
                has_ready_endpoints=False,
            )
        ],
    )


class SecondarySymptomPruningTests(unittest.TestCase):
    def assert_root_kinds(
        self,
        graph: DependencyGraph,
        expected: set[str],
    ) -> None:
        self.assertEqual(
            expected,
            {result.root_cause_kind for result in graph.find_root_causes()},
        )
        self.assertEqual(
            expected,
            {
                result.root_cause_kind
                for result in graph.find_root_causes_dfs_only()
            },
        )

    def test_missing_secret_prunes_explained_rollout_and_service(self) -> None:
        pod = make_pod(
            "catalogue-new",
            owner=("ReplicaSet", "catalogue-rs"),
            status="ERROR",
            reason="CreateContainerConfigError",
            phase="Pending",
            labels={"name": "catalogue"},
            secret_names=["missing-secret"],
        )

        graph = DependencyGraph(aggregate_topology(pod))

        self.assert_root_kinds(graph, {"Secret"})

    def test_missing_pvc_prunes_explained_replicaset_and_deployment(self) -> None:
        pod = make_pod(
            "catalogue-new",
            owner=("ReplicaSet", "catalogue-rs"),
            status="WARNING",
            reason="Pending",
            phase="Pending",
            labels={"name": "catalogue"},
            pvc_names=["missing-pvc"],
        )

        graph = DependencyGraph(aggregate_topology(pod))

        self.assert_root_kinds(graph, {"PVC"})

    def test_independent_multifault_roots_are_preserved(self) -> None:
        payment = make_pod(
            "payment-new",
            owner=("ReplicaSet", "payment-rs"),
            status="ERROR",
            reason="ReadinessProbeFailed",
            labels={"name": "payment"},
        )
        cache = make_cache(
            pods=[payment],
            replica_sets=[
                MiniReplicaSet(
                    name="payment-rs",
                    namespace="sock-shop",
                    desired=1,
                    ready=0,
                    owner_name="payment",
                )
            ],
            deployments=[
                MiniDeployment(
                    name="payment",
                    namespace="sock-shop",
                    desired=1,
                    available=1,
                    ready=1,
                )
            ],
            services=[
                MiniService(
                    name="user",
                    namespace="sock-shop",
                    selector={"name": "user-invalid"},
                    cluster_ip="10.0.0.20",
                    svc_type="ClusterIP",
                    has_ready_endpoints=False,
                )
            ],
        )

        self.assert_root_kinds(
            DependencyGraph(cache),
            {"ReplicaSet", "Service"},
        )

    def test_missing_secret_does_not_hide_unrelated_service(self) -> None:
        pod = make_pod(
            "catalogue-new",
            owner=("ReplicaSet", "catalogue-rs"),
            status="ERROR",
            reason="CreateContainerConfigError",
            phase="Pending",
            labels={"name": "catalogue"},
            secret_names=["missing-secret"],
        )
        cache = aggregate_topology(pod)
        cache.services.append(
            MiniService(
                name="user",
                namespace="sock-shop",
                selector={"name": "user-invalid"},
                cluster_ip="10.0.0.20",
                svc_type="ClusterIP",
                has_ready_endpoints=False,
            )
        )

        self.assert_root_kinds(
            DependencyGraph(cache),
            {"Secret", "Service"},
        )

    def test_mixed_rollout_keeps_aggregate_symptoms(self) -> None:
        missing_secret = make_pod(
            "catalogue-missing-secret",
            owner=("ReplicaSet", "catalogue-rs"),
            status="ERROR",
            reason="CreateContainerConfigError",
            phase="Pending",
            labels={"name": "catalogue"},
            secret_names=["missing-secret"],
        )
        image_failure = make_pod(
            "catalogue-image-failure",
            owner=("ReplicaSet", "catalogue-rs"),
            status="ERROR",
            reason="ImagePullBackOff",
            phase="Pending",
            labels={"name": "catalogue"},
        )
        cache = aggregate_topology(missing_secret)
        cache.pods.append(image_failure)

        graph = DependencyGraph(cache)
        for results in (
            graph.find_root_causes(),
            graph.find_root_causes_dfs_only(),
        ):
            kinds = {result.root_cause_kind for result in results}
            self.assertIn("Secret", kinds)
            self.assertIn("Deployment", kinds)
            self.assertIn("Service", kinds)

    def test_service_selector_uses_exact_pod_labels(self) -> None:
        catalogue = make_pod(
            "catalogue-123",
            labels={"name": "catalogue"},
        )
        catalogue_db = make_pod(
            "catalogue-db-456",
            labels={"name": "catalogue-db"},
        )
        service = MiniService(
            name="catalogue",
            namespace="sock-shop",
            selector={"name": "catalogue"},
            cluster_ip="10.0.0.10",
            svc_type="ClusterIP",
            has_ready_endpoints=True,
        )

        graph = DependencyGraph(
            make_cache(pods=[catalogue, catalogue_db], services=[service])
        )

        edges = graph.forward_edges["Service/sock-shop/catalogue"]
        self.assertIn("Pod/sock-shop/catalogue-123", edges)
        self.assertNotIn("Pod/sock-shop/catalogue-db-456", edges)


if __name__ == "__main__":
    unittest.main()
