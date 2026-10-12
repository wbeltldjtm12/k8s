"""Ensure normalized network probe evidence participates in DFS RCA."""

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
from infra.network_evidence import NetworkEvidence


def make_cache() -> SimpleNamespace:
    pod = MiniPod(
        name="shipping-abc",
        namespace="sock-shop",
        phase="Running",
        node_name="worker1",
        owner=None,
        containers=[],
        pvc_names=[],
        cm_names=[],
        secret_names=[],
        labels={"name": "shipping"},
    )
    return SimpleNamespace(
        nodes=[], pods=[pod], deployments=[], replica_sets=[], daemon_sets=[],
        stateful_sets=[], services=[], ingresses=[], pvcs=[], configmaps=[],
        secrets=[], storage_classes=set(), hpas=[], jobs=[], cronjobs=[], pvs=[],
        cm_exists=set(), secret_exists=set(),
    )


def make_unready_payment_cache(
    *,
    mixed_failure: bool = False,
    source_pod_recovered: bool = False,
) -> SimpleNamespace:
    pods = [
        MiniPod(
            name="payment-abc",
            namespace="sock-shop",
            phase="Running",
            node_name="worker1",
            owner=("ReplicaSet", "payment-rs"),
            containers=[],
            pvc_names=[],
            cm_names=[],
            secret_names=[],
            labels={"name": "payment"},
            status="OK" if source_pod_recovered else "WARNING",
            error_reason="" if source_pod_recovered else "NotReady",
        )
    ]
    if mixed_failure:
        pods.append(
            MiniPod(
                name="payment-independent",
                namespace="sock-shop",
                phase="Running",
                node_name="worker2",
                owner=("ReplicaSet", "payment-rs"),
                containers=[],
                pvc_names=[],
                cm_names=[],
                secret_names=[],
                labels={"name": "payment"},
                status="WARNING",
                error_reason="CrashLoopBackOff",
            )
        )
    return SimpleNamespace(
        nodes=[],
        pods=pods,
        deployments=[
            MiniDeployment(
                name="payment",
                namespace="sock-shop",
                desired=len(pods),
                available=0,
                ready=0,
                status="ERROR",
                error_reason="NoAvailableReplicas",
            )
        ],
        replica_sets=[
            MiniReplicaSet(
                name="payment-rs",
                namespace="sock-shop",
                desired=len(pods),
                ready=0,
                owner_name="payment",
            )
        ],
        daemon_sets=[],
        stateful_sets=[],
        services=[
            MiniService(
                name="payment",
                namespace="sock-shop",
                selector={"name": "payment"},
                cluster_ip="10.96.0.10",
                svc_type="ClusterIP",
                has_ready_endpoints=False,
            )
        ],
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


class NetworkPathGraphTests(unittest.TestCase):
    def test_failed_dns_probe_is_root_cause_and_affects_source_pod(self) -> None:
        graph = DependencyGraph(make_cache())
        graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="shipping",
                source_pod="shipping-abc",
                target="catalogue",
                probe="dns",
                status="ERROR",
                reason="DNSProbeFailed",
                message="shipping DNS probe to catalogue failed",
            )
        ])

        node_id = (
            "NetworkPath/sock-shop/"
            "shipping->catalogue[dns]@shipping-abc"
        )
        self.assertIn(node_id, graph.nodes)
        self.assertIn(node_id, graph.forward_edges["Pod/sock-shop/shipping-abc"])

        results = graph.find_root_causes()
        self.assertEqual("NetworkPath", results[0].root_cause_kind)
        self.assertEqual("DNSProbeFailed", results[0].root_cause_reason)
        self.assertIn("Pod/sock-shop/shipping-abc", results[0].affected_node_ids)

    def test_stale_source_pod_does_not_create_an_orphan_root(self) -> None:
        graph = DependencyGraph(make_cache())

        added = graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="shipping",
                source_pod="shipping-old",
                target="catalogue",
                probe="dns",
                status="ERROR",
                reason="DNSProbeFailed",
                message="stale Pod evidence",
            )
        ])

        self.assertEqual(0, added)
        self.assertFalse(
            any(node.kind == "NetworkPath" for node in graph.nodes.values())
        )

    def test_evidence_is_attached_only_to_the_exact_current_pod(self) -> None:
        cache = make_cache()
        cache.pods.append(
            MiniPod(
                name="shipping-def",
                namespace="sock-shop",
                phase="Running",
                node_name="worker2",
                owner=None,
                containers=[],
                pvc_names=[],
                cm_names=[],
                secret_names=[],
                labels={"name": "shipping"},
            )
        )
        graph = DependencyGraph(cache)
        graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="shipping",
                source_pod="shipping-abc",
                target="catalogue",
                probe="dns",
                status="ERROR",
                reason="DNSProbeFailed",
                message="current Pod evidence",
            )
        ])

        node_id = (
            "NetworkPath/sock-shop/"
            "shipping->catalogue[dns]@shipping-abc"
        )
        self.assertIn(
            node_id,
            graph.forward_edges["Pod/sock-shop/shipping-abc"],
        )
        self.assertNotIn(
            node_id,
            graph.forward_edges["Pod/sock-shop/shipping-def"],
        )

    def test_two_current_replicas_create_distinct_network_paths(self) -> None:
        cache = make_cache()
        cache.pods.append(
            MiniPod(
                name="shipping-def",
                namespace="sock-shop",
                phase="Running",
                node_name="worker2",
                owner=None,
                containers=[],
                pvc_names=[],
                cm_names=[],
                secret_names=[],
                labels={"name": "shipping"},
            )
        )
        graph = DependencyGraph(cache)

        added = graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="shipping",
                source_pod=source_pod,
                target="catalogue",
                probe="dns",
                status="ERROR",
                reason="DNSProbeFailed",
                message=f"{source_pod} DNS failure",
            )
            for source_pod in ("shipping-abc", "shipping-def")
        ])

        self.assertEqual(2, added)
        self.assertEqual(
            {
                "shipping->catalogue[dns]@shipping-abc",
                "shipping->catalogue[dns]@shipping-def",
            },
            {
                node.name
                for node in graph.nodes.values()
                if node.kind == "NetworkPath"
            },
        )

    def test_network_root_prunes_its_rollout_and_endpoint_symptoms(self) -> None:
        graph = DependencyGraph(make_unready_payment_cache())
        graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="payment",
                source_pod="payment-abc",
                target="payment",
                probe="http",
                status="WARNING",
                reason="HighNetworkLatency",
                message="payment HTTP probe latency is high",
            )
        ])

        for results in (
            graph.find_root_causes(),
            graph.find_root_causes_dfs_only(),
        ):
            with self.subTest(result_count=len(results)):
                self.assertEqual(1, len(results))
                self.assertEqual(
                    "NetworkPath",
                    results[0].root_cause_kind,
                )
                self.assertEqual(
                    "HighNetworkLatency",
                    results[0].root_cause_reason,
                )

    def test_network_root_does_not_hide_mixed_pod_failure(self) -> None:
        graph = DependencyGraph(
            make_unready_payment_cache(mixed_failure=True)
        )
        graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="payment",
                source_pod="payment-abc",
                target="payment",
                probe="http",
                status="WARNING",
                reason="HighNetworkLatency",
                message="payment HTTP probe latency is high",
            )
        ])

        for results in (
            graph.find_root_causes(),
            graph.find_root_causes_dfs_only(),
        ):
            root_kinds = {
                result.root_cause_kind
                for result in results
            }
            self.assertIn("NetworkPath", root_kinds)
            self.assertIn("Deployment", root_kinds)
            self.assertIn("Service", root_kinds)

    def test_network_root_prunes_lagging_aggregates_after_pod_recovers(
        self,
    ) -> None:
        graph = DependencyGraph(
            make_unready_payment_cache(source_pod_recovered=True)
        )
        graph.add_network_evidence([
            NetworkEvidence(
                namespace="sock-shop",
                source_workload="payment",
                source_pod="payment-abc",
                target="payment",
                probe="http",
                status="WARNING",
                reason="HighNetworkLatency",
                message="recent payment HTTP latency",
            )
        ])

        for results in (
            graph.find_root_causes(),
            graph.find_root_causes_dfs_only(),
        ):
            self.assertEqual(
                ["NetworkPath"],
                [result.root_cause_kind for result in results],
            )


if __name__ == "__main__":
    unittest.main()
