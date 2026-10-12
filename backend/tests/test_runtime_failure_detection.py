"""Regression tests for direct runtime failures and zero-replica handling."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from analysis.graph import DependencyGraph
from infra.cache import ResourceCache
from infra.models import MiniDeployment, MiniPod, MiniReplicaSet, MiniService


def namespace(**values):
    return SimpleNamespace(**values)


class RuntimeFailureDetectionTests(unittest.TestCase):
    def test_oomkilled_last_state_is_preserved_as_pod_error(self) -> None:
        container_spec = namespace(name="orders", image="orders:test", env=[], env_from=[])
        container_status = namespace(
            name="orders",
            state=namespace(
                waiting=namespace(reason="CrashLoopBackOff", message="back-off"),
                terminated=None,
            ),
            last_state=namespace(
                terminated=namespace(reason="OOMKilled", exit_code=137)
            ),
            restart_count=2,
        )
        pod = namespace(
            metadata=namespace(
                name="orders-test",
                namespace="sock-shop",
                owner_references=[],
                labels={"name": "orders"},
            ),
            spec=namespace(
                node_name="",
                containers=[container_spec],
                init_containers=[],
                volumes=[],
            ),
            status=namespace(
                phase="Running",
                reason="",
                container_statuses=[container_status],
                init_container_statuses=[],
            ),
        )

        slim = ResourceCache.__new__(ResourceCache)._slim_pod(pod)

        self.assertEqual("ERROR", slim.status)
        self.assertEqual("OOMKilled", slim.error_reason)
        self.assertEqual(
            "OOMKilled",
            slim.containers[0].last_terminated_reason,
        )
        self.assertEqual(137, slim.containers[0].last_exit_code)

    def test_oomkilled_pod_outranks_rollout_symptoms(self) -> None:
        pod = MiniPod(
            name="orders-test",
            namespace="sock-shop",
            phase="Running",
            node_name="",
            owner=("ReplicaSet", "orders-rs"),
            containers=[],
            pvc_names=[],
            cm_names=[],
            secret_names=[],
            status="ERROR",
            error_reason="OOMKilled",
        )
        healthy_pods = [
            MiniPod(
                name=f"orders-old-{index}",
                namespace="sock-shop",
                phase="Running",
                node_name="",
                owner=("ReplicaSet", "orders-rs"),
                containers=[],
                pvc_names=[],
                cm_names=[],
                secret_names=[],
                labels={"name": "orders"},
            )
            for index in (1, 2)
        ]
        pod.labels = {"name": "orders"}
        cache = namespace(
            nodes=[],
            pods=[pod, *healthy_pods],
            deployments=[
                MiniDeployment(
                    name="orders",
                    namespace="sock-shop",
                    desired=1,
                    available=0,
                    ready=0,
                    status="ERROR",
                    error_reason="NoAvailableReplicas",
                )
            ],
            replica_sets=[
                MiniReplicaSet(
                    name="orders-rs",
                    namespace="sock-shop",
                    desired=1,
                    ready=0,
                    owner_name="orders",
                )
            ],
            daemon_sets=[],
            stateful_sets=[],
            services=[
                MiniService(
                    name="orders",
                    namespace="sock-shop",
                    selector={"name": "orders"},
                    cluster_ip="10.0.0.1",
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

        graph = DependencyGraph(cache)
        hybrid_results = graph.find_root_causes()
        dfs_results = graph.find_root_causes_dfs_only()

        deployment_result = next(
            result
            for result in hybrid_results
            if result.root_cause_kind == "Deployment"
        )
        pod_result = next(
            result
            for result in hybrid_results
            if result.root_cause_kind == "Pod"
        )
        self.assertGreater(deployment_result.score, pod_result.score)
        self.assertEqual("Pod", hybrid_results[0].root_cause_kind)
        self.assertEqual("orders-test", hybrid_results[0].root_cause_name)
        self.assertEqual("OOMKilled", hybrid_results[0].root_cause_reason)
        self.assertEqual("Pod", dfs_results[0].root_cause_kind)
        self.assertEqual("orders-test", dfs_results[0].root_cause_name)
        self.assertEqual("OOMKilled", dfs_results[0].root_cause_reason)

    def test_zero_replicas_is_not_coerced_to_one(self) -> None:
        deployment = namespace(
            metadata=namespace(name="catalogue", namespace="sock-shop"),
            spec=namespace(replicas=0),
            status=namespace(available_replicas=0, ready_replicas=0),
        )

        slim = ResourceCache.__new__(ResourceCache)._slim_deploy(deployment)

        self.assertEqual(0, slim.desired)
        self.assertEqual("OK", slim.status)
        self.assertEqual("", slim.error_reason)


if __name__ == "__main__":
    unittest.main()
