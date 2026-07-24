"""
analysis/graph.py
====================
쿠버네티스 인메모리 의존성 그래프 생성 & 근본 원인 분석기 (경량 데이터 모델 기반)

흐름: 그래프 빌드 → 리소스 등록 → 엣지 연결 → find_root_causes()
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from infra.models import (
    MiniPod, MiniDeployment, MiniReplicaSet,
    MiniStatefulSet, MiniDaemonSet, MiniService, MiniIngress,
    MiniPVC, MiniNode, MiniConfigMap, MiniSecret,
    MiniHPA, MiniJob, MiniCronJob, MiniPV,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 가중치 상수
# ──────────────────────────────────────────────
W1 = 10   # 우선순위 가중치
W2 = 1    # 파급 범위 가중치
W3 = 2    # 에러 깊이 가중치
W4 = 5    # 상태 가중치

PRIORITY_MISSING   = 1   # 리소스 누락
PRIORITY_INFRA     = 2   # 인프라 (노드)
PRIORITY_STORAGE   = 3   # 스토리지 (PVC, SC)
PRIORITY_WORKLOAD  = 4   # 워크로드 (Pod, Deploy, RS 등)
PRIORITY_UPPER     = 5   # 상위 리소스 (Service, Ingress)

STATUS_WEIGHT = {
    "ERROR":   10,
    "WARNING":  3,
    "OK":       0,
}

STATUS_ORDER = {          # 최종 정렬용 (낮을수록 먼저 표시)
    "ERROR":   0,
    "WARNING": 1,
    "OK":      2,
}

SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "local-path-storage", "chaos-mesh"}

INFRA_COMPONENTS = {
    ("kube-system", "flannel"):    "all_nodes",
    ("kube-system", "calico"):     "all_nodes",
    ("kube-system", "cilium"):     "all_nodes",
    ("kube-system", "coredns"):    "all_pods",
    ("kube-system", "kube-proxy"): "all_services",
}

# ──────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────
@dataclass
class GraphNode:
    """그래프 내 단일 리소스 노드"""
    id: str                          # "Kind/namespace/name" 또는 "Kind/name" (클러스터 스코프)
    kind: str = ""
    name: str = ""
    namespace: str = ""
    status: str = "OK"               # OK | WARNING | ERROR
    error_reason: str = ""
    error_message: str = ""
    priority: int = PRIORITY_UPPER
    is_system: bool = False
    raw: Optional[object] = field(default=None, repr=False)


@dataclass
class RCAResult:
    """근본 원인 분석 결과 하나."""
    root_cause: Optional[GraphNode] = None
    root_cause_kind: str = ""
    root_cause_name: str = ""
    root_cause_namespace: str = ""
    root_cause_reason: str = ""
    root_cause_priority: int = PRIORITY_UPPER
    root_cause_status: str = "OK"
    chain: List[GraphNode] = field(default_factory=list)
    chain_summary: str = ""
    depth: int = 0
    blast_radius: int = 0
    score: float = 0.0
    affected_node_ids: List[str] = field(default_factory=list)
    affected_nodes: List[GraphNode] = field(default_factory=list)

    def build_summary(self) -> str:
        """전파 체인을 사람이 읽을 수 있는 문자열로 변환."""
        lines: List[str] = []
        for n in self.chain:
            status_icon = {"ERROR": "[ERR]", "WARNING": "[WAR]", "OK": "[OK]"}.get(n.status, "[MIS]")
            reason_part = f" ({n.error_reason})" if n.error_reason else ""
            lines.append(f"{status_icon} {n.kind}/{n.namespace}/{n.name}{reason_part}")
        self.chain_summary = " → ".join(lines)
        return self.chain_summary


# ──────────────────────────────────────────────
# 의존성 그래프 메인 클래스
# ──────────────────────────────────────────────
class DependencyGraph:
    """
    ResourceCache 데이터를 사용하여 의존성 그래프를 구축하고,
    find_root_causes()로 근본 원인을 분석합니다.
    """

    def __init__(self, cache=None):
        self.nodes: Dict[str, GraphNode] = {}
        self.forward_edges: Dict[str, Set[str]] = {}
        self.reverse_edges: Dict[str, Set[str]] = {}

        if cache is not None:
            self.build_graph(cache)

    def _node_id(self, kind: str, namespace: str, name: str) -> str:
        """노드 고유 ID 생성."""
        if namespace:
            return f"{kind}/{namespace}/{name}"
        return f"{kind}/{name}"

    def _add_node(self, node: GraphNode) -> None:
        """그래프에 노드 추가"""
        self.nodes[node.id] = node
        self.forward_edges.setdefault(node.id, set())
        self.reverse_edges.setdefault(node.id, set())

    def _add_edge(self, from_id: str, to_id: str) -> None:
        """의존성 엣지 추가. from_id가 to_id에 의존함을 의미"""
        self.forward_edges.setdefault(from_id, set()).add(to_id)
        self.reverse_edges.setdefault(to_id, set()).add(from_id)

    # ─── 리소스 등록 및 그래프 구축 ─────────────
    def build_graph(self, cache) -> None:
        """ResourceCache 객체에서 전체 그래프를 구축한다."""
        self._register_nodes(cache.nodes)
        self._register_pods(cache.pods)
        self._register_deployments(cache.deployments)
        self._register_replicasets(cache.replica_sets)
        self._register_daemonsets(cache.daemon_sets)
        self._register_statefulsets(cache.stateful_sets)
        self._register_services(cache.services)
        self._register_ingresses(cache.ingresses)
        self._register_pvcs(cache.pvcs)
        self._register_configmaps(cache.configmaps)
        self._register_secrets(cache.secrets)
        self._register_storageclasses(cache.storage_classes)

        self._link_pod_edges(cache.pods)
        self._link_ownership_edges(cache.pods, cache.replica_sets)
        self._link_service_edges(cache.services, cache.pods)
        self._link_ingress_edges(cache.ingresses)
        self._link_pvc_edges(cache.pvcs)

        self._detect_missing_resources(cache)

        # 신규 리소스 등록
        self._register_hpas(cache.hpas)
        self._register_jobs(cache.jobs)
        self._register_cronjobs(cache.cronjobs)
        self._register_pvs(cache.pvs)

        # 신규 엣지 링킹
        self._link_hpa_edges(cache.hpas)
        self._link_job_edges(cache.pods)
        self._link_cronjob_edges(cache.jobs)
        self._link_pv_edges(cache.pvs)

        # 인프라 의존성 (조건부)
        self._link_infra_dependencies(cache)

        logger.info(f"그래프 빌드 완료: 노드 {len(self.nodes)}개, "
                     f"엣지 {sum(len(v) for v in self.forward_edges.values())}개")

    # ─── 개별 리소스 등록 ─────────────────────

    def _register_nodes(self, items: List[MiniNode]) -> None:
        for item in items:
            nid = self._node_id("Node", "", item.name)
            node = GraphNode(
                id=nid, kind="Node", name=item.name, namespace="",
                priority=PRIORITY_INFRA, raw=item,
                is_system=("control-plane" in item.name)
            )
            self._eval_node(node, item)
            self._add_node(node)

    def _register_pods(self, items: List[MiniPod]) -> None:
        for item in items:
            nid = self._node_id("Pod", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="Pod", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_pod(node, item)
            self._add_node(node)

    def _register_deployments(self, items: List[MiniDeployment]) -> None:
        for item in items:
            nid = self._node_id("Deployment", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="Deployment", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_deployment(node, item)
            self._add_node(node)

    def _register_replicasets(self, items: List[MiniReplicaSet]) -> None:
        for item in items:
            nid = self._node_id("ReplicaSet", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="ReplicaSet", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_rs(node, item)
            self._add_node(node)

    def _register_daemonsets(self, items: List[MiniDaemonSet]) -> None:
        for item in items:
            nid = self._node_id("DaemonSet", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="DaemonSet", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_daemonset(node, item)
            self._add_node(node)

    def _register_statefulsets(self, items: List[MiniStatefulSet]) -> None:
        for item in items:
            nid = self._node_id("StatefulSet", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="StatefulSet", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_statefulset(node, item)
            self._add_node(node)

    def _register_services(self, items: List[MiniService]) -> None:
        for item in items:
            nid = self._node_id("Service", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="Service", name=item.name, namespace=item.namespace,
                priority=PRIORITY_UPPER, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._eval_service(node, item)
            self._add_node(node)

    def _register_ingresses(self, items: List[MiniIngress]) -> None:
        for item in items:
            nid = self._node_id("Ingress", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="Ingress", name=item.name, namespace=item.namespace,
                priority=PRIORITY_UPPER, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._add_node(node)

    def _register_pvcs(self, items: List[MiniPVC]) -> None:
        for item in items:
            nid = self._node_id("PVC", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="PVC", name=item.name, namespace=item.namespace,
                priority=PRIORITY_STORAGE, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            if item.phase == "Pending":
                node.status = "WARNING"
                node.error_reason = "Pending"
                node.error_message = "PVC가 아직 바인딩되지 않음"
            elif item.phase == "Lost":
                node.status = "ERROR"
                node.error_reason = "Lost"
                node.error_message = "PVC가 PV와의 연결을 잃음"
            self._add_node(node)

    def _register_configmaps(self, items: List[MiniConfigMap]) -> None:
        for item in items:
            nid = self._node_id("ConfigMap", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="ConfigMap", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._add_node(node)

    def _register_secrets(self, items: List[MiniSecret]) -> None:
        for item in items:
            nid = self._node_id("Secret", item.namespace, item.name)
            node = GraphNode(
                id=nid, kind="Secret", name=item.name, namespace=item.namespace,
                priority=PRIORITY_WORKLOAD, raw=item,
                is_system=(item.namespace in SYSTEM_NAMESPACES)
            )
            self._add_node(node)

    def _register_storageclasses(self, items: Set[str]) -> None:
        for name in items:
            nid = self._node_id("StorageClass", "", name)
            node = GraphNode(
                id=nid, kind="StorageClass", name=name, namespace="",
                priority=PRIORITY_STORAGE, raw=name,
                is_system=False
            )
            self._add_node(node)

    # ─── 상태 평가 함수들 ─────────────────────

    def _eval_node(self, gnode: GraphNode, item: MiniNode) -> None:
        if not item.ready:
            gnode.status = "ERROR"
            gnode.error_reason = "NotReady"
            gnode.error_message = "노드가 Ready 상태가 아님"
            return
        if item.pressures:
            gnode.status = "WARNING"
            gnode.error_reason = ",".join(item.pressures)
            gnode.error_message = f"{','.join(item.pressures)} 상태가 감지됨"
            return
        gnode.status = "OK"

    def _eval_pod(self, gnode: GraphNode, item: MiniPod) -> None:
        gnode.status = item.status
        gnode.error_reason = item.error_reason
        gnode.error_message = f"Phase: {item.phase}"
        if item.error_reason:
            gnode.error_message += f", Reason: {item.error_reason}"

    def _eval_deployment(self, gnode: GraphNode, item: MiniDeployment) -> None:
        gnode.status = item.status
        gnode.error_reason = item.error_reason
        gnode.error_message = f"Desired: {item.desired}, Available: {item.available}, Ready: {item.ready}"

    def _eval_rs(self, gnode: GraphNode, item: MiniReplicaSet) -> None:
        if item.desired > 0 and item.ready < item.desired:
            gnode.status = "WARNING"
            gnode.error_reason = "ReplicasMismatch"
            gnode.error_message = f"원하는 레플리카 {item.desired}개 중 {item.ready}개만 준비됨"
        else:
            gnode.status = "OK"

    def _eval_daemonset(self, gnode: GraphNode, item: MiniDaemonSet) -> None:
        if item.ready < item.desired:
            gnode.status = "WARNING"
            gnode.error_reason = "NotAllReady"
            gnode.error_message = f"원하는 {item.desired}개 중 {item.ready}개만 준비됨"
        else:
            gnode.status = "OK"

    def _eval_statefulset(self, gnode: GraphNode, item: MiniStatefulSet) -> None:
        if item.ready < item.desired:
            gnode.status = "WARNING"
            gnode.error_reason = "ReplicasMismatch"
            gnode.error_message = f"원하는 레플리카 {item.desired}개 중 {item.ready}개만 준비됨"
        else:
            gnode.status = "OK"

    def _eval_service(self, gnode: GraphNode, item: MiniService) -> None:
        if item.namespace == "kube-system":
            return
        if item.cluster_ip == "None":
            return
        if not item.has_ready_endpoints:
            gnode.status = "WARNING"
            gnode.error_reason = "NoEndpoints"
            gnode.error_message = "Service에 연결된 Ready Endpoint가 없음"

    # ─── 엣지 연결 ────────────────────────────

    def _link_pod_edges(self, pods: List[MiniPod]) -> None:
        for pod in pods:
            pod_id = self._node_id("Pod", pod.namespace, pod.name)
            if pod.node_name:
                node_id = self._node_id("Node", "", pod.node_name)
                self._add_edge(pod_id, node_id)
            for pvc_name in pod.pvc_names:
                pvc_id = self._node_id("PVC", pod.namespace, pvc_name)
                self._add_edge(pod_id, pvc_id)
            for cm_name in pod.cm_names:
                cm_id = self._node_id("ConfigMap", pod.namespace, cm_name)
                self._add_edge(pod_id, cm_id)
            for secret_name in pod.secret_names:
                secret_id = self._node_id("Secret", pod.namespace, secret_name)
                self._add_edge(pod_id, secret_id)

    def _link_ownership_edges(self, pods: List[MiniPod],
                              replicasets: List[MiniReplicaSet]) -> None:
        for pod in pods:
            pod_id = self._node_id("Pod", pod.namespace, pod.name)
            if pod.owner:
                owner_kind, owner_name = pod.owner
                owner_id = self._node_id(owner_kind, pod.namespace, owner_name)
                self._add_edge(pod_id, owner_id)
        for rs in replicasets:
            rs_id = self._node_id("ReplicaSet", rs.namespace, rs.name)
            if rs.owner_name:
                deploy_id = self._node_id("Deployment", rs.namespace, rs.owner_name)
                self._add_edge(rs_id, deploy_id)

    def _link_service_edges(self, services: List[MiniService], pods: List[MiniPod]) -> None:
        for svc in services:
            svc_id = self._node_id("Service", svc.namespace, svc.name)
            if not svc.selector:
                continue
            for pod in pods:
                if pod.namespace != svc.namespace:
                    continue
                matched = True
                for v in svc.selector.values():
                    if v.lower() not in pod.name.lower():
                        matched = False
                        break
                if matched:
                    pod_id = self._node_id("Pod", pod.namespace, pod.name)
                    self._add_edge(svc_id, pod_id)

    def _link_ingress_edges(self, ingresses: List[MiniIngress]) -> None:
        for ing in ingresses:
            ing_id = self._node_id("Ingress", ing.namespace, ing.name)
            for svc_name in ing.backend_services:
                svc_id = self._node_id("Service", ing.namespace, svc_name)
                self._add_edge(ing_id, svc_id)

    def _link_pvc_edges(self, pvcs: List[MiniPVC]) -> None:
        for pvc in pvcs:
            pvc_id = self._node_id("PVC", pvc.namespace, pvc.name)
            if pvc.storage_class:
                sc_id = self._node_id("StorageClass", "", pvc.storage_class)
                self._add_edge(pvc_id, sc_id)

    # ─── 누락 리소스 감지 ─────────────────────

    @staticmethod
    def _should_promote_missing_dependency(pod: MiniPod) -> bool:
        """Return true only when a missing dependency is an active startup failure.

        A ConfigMap or Secret can disappear after a Pod has already mounted it.
        The Pod may remain healthy, especially when an operator rotates generated
        configuration. Treating every historical reference as ERROR makes a
        healthy baseline look faulty. Terminal Pods are historical evidence, and
        a live dependency is promoted only when kubelet explicitly reports the
        configuration-startup failure.
        """
        return (
            pod.phase not in {"Succeeded", "Failed"}
            and pod.status == "ERROR"
            and pod.error_reason == "CreateContainerConfigError"
        )

    def _add_missing_dependency(
        self,
        pod: MiniPod,
        kind: str,
        dependency_name: str,
    ) -> None:
        pod_id = self._node_id("Pod", pod.namespace, pod.name)
        dep_id = self._node_id(kind, pod.namespace, dependency_name)
        missing_node = GraphNode(
            id=dep_id,
            kind=kind,
            name=dependency_name,
            namespace=pod.namespace,
            status="ERROR",
            error_reason="Missing",
            error_message=(
                f"{kind}/{dependency_name} 리소스가 클러스터에 존재하지 않음"
            ),
            priority=PRIORITY_MISSING,
            is_system=False,
        )
        self._add_node(missing_node)
        self._add_edge(pod_id, dep_id)
        logger.warning(f"[WARN] 시작 실패 원인의 누락 리소스 감지: {dep_id}")

    def _detect_missing_resources(self, cache) -> None:
        """Promote only active Pod startup failures caused by a missing dependency.

        All dependency edges are retained by _link_pod_edges(). A missing
        ConfigMap, Secret, or PVC becomes an ERROR node only when the current
        Pod is explicitly failing with CreateContainerConfigError.
        """
        pvc_exists = {(p.namespace, p.name) for p in cache.pvcs}

        for pod in cache.pods:
            if not self._should_promote_missing_dependency(pod):
                continue

            for cm_name in pod.cm_names:
                if (pod.namespace, cm_name) not in cache.cm_exists:
                    self._add_missing_dependency(pod, "ConfigMap", cm_name)

            for secret_name in pod.secret_names:
                if (pod.namespace, secret_name) not in cache.secret_exists:
                    self._add_missing_dependency(pod, "Secret", secret_name)

            for pvc_name in pod.pvc_names:
                if (pod.namespace, pvc_name) not in pvc_exists:
                    self._add_missing_dependency(pod, "PVC", pvc_name)

    # ─── 근본 원인 분석 ───────────────────────

    def find_root_causes(self) -> List[RCAResult]:
        """
        에러/경고 상태의 노드들을 시작점으로 DFS를 수행해
        가장 깊은 (우선순위가 높은) 근본 원인을 찾고,
        역방향 BFS로 파급 범위를 계산한다.
        """
        if not self.nodes:
            print("[INFO] 그래프 노드 없음 - RCA 스킵")
            return []

        error_nodes = [
            nid for nid, n in self.nodes.items()
            if n.status in ("ERROR", "WARNING") and not n.is_system
        ]

        if not error_nodes:
            logger.info("[INFO] 에러 노드가 없음 — 근본 원인 없음")
            return []

        visited_roots: Set[str] = set()
        results: List[RCAResult] = []

        for start_id in error_nodes:
            roots = self._dfs_find_root(start_id)
            for root_id, chain_ids in roots:
                if root_id in visited_roots:
                    continue
                visited_roots.add(root_id)

                blast_radius, depth, affected_ids = self._reverse_bfs(root_id)

                root_node = self.nodes[root_id]

                sw = STATUS_WEIGHT.get(root_node.status, 0)
                score = (W1 * (1.0 / root_node.priority)
                         + W2 * blast_radius
                         + W3 * depth
                         + W4 * sw)
                score = round(score, 2)

                chain_nodes = [
                    self.nodes[nid] for nid in chain_ids if nid in self.nodes
                ]

                affected_node_objects = [
                    self.nodes[nid] for nid in affected_ids if nid in self.nodes
                ]

                result = RCAResult(
                    root_cause=root_node,
                    root_cause_kind=root_node.kind,
                    root_cause_name=root_node.name,
                    root_cause_namespace=root_node.namespace,
                    root_cause_reason=root_node.error_reason,
                    root_cause_priority=root_node.priority,
                    root_cause_status=root_node.status,
                    chain=chain_nodes,
                    depth=depth,
                    blast_radius=blast_radius,
                    score=score,
                    affected_node_ids=affected_ids,
                    affected_nodes=affected_node_objects,
                )
                result.build_summary()
                results.append(result)

        results.sort(key=lambda r: (
            STATUS_ORDER.get(r.root_cause_status, 2),
            -r.score
        ))

        logger.info(f"[INFO] 근본 원인 {len(results)}개 발견")
        return results

    def find_root_causes_dfs_only(self) -> List[RCAResult]:
        """DFS만 수행, blast_radius=0 고정. BFS 없이 priority 기반 랭킹만 사용."""
        error_nodes = [
            nid for nid, n in self.nodes.items()
            if n.status in ("ERROR", "WARNING") and not n.is_system
        ]
        if not error_nodes:
            return []

        visited_roots: Set[str] = set()
        results: List[RCAResult] = []

        for start_id in error_nodes:
            roots = self._dfs_find_root(start_id)
            for root_id, chain_ids in roots:
                if root_id in visited_roots:
                    continue
                visited_roots.add(root_id)

                root_node = self.nodes[root_id]
                depth = len(chain_ids) - 1
                sw = STATUS_WEIGHT.get(root_node.status, 0)
                score = (W1 * (1.0 / root_node.priority)
                         + W2 * 0
                         + W3 * depth
                         + W4 * sw)
                score = round(score, 2)

                chain_nodes = [self.nodes[nid] for nid in chain_ids if nid in self.nodes]

                result = RCAResult(
                    root_cause=root_node,
                    root_cause_kind=root_node.kind,
                    root_cause_name=root_node.name,
                    root_cause_namespace=root_node.namespace,
                    root_cause_reason=root_node.error_reason,
                    root_cause_priority=root_node.priority,
                    root_cause_status=root_node.status,
                    chain=chain_nodes,
                    depth=depth,
                    blast_radius=0,
                    score=score,
                    affected_node_ids=[],
                    affected_nodes=[],
                )
                result.build_summary()
                results.append(result)

        results.sort(key=lambda r: (STATUS_ORDER.get(r.root_cause_status, 2), -r.score))
        return results

    def _dfs_find_root(self, start_id: str) -> List[Tuple[str, List[str]]]:
        visited: Set[str] = set()
        stack: List[Tuple[str, List[str]]] = [(start_id, [start_id])]
        roots_by_prio: Dict[int, Tuple[str, List[str]]] = {}

        while stack:
            current, path = stack.pop()
            if current in visited:
                continue
            # 무한루프 방지: 경로 최대 깊이 50
            if len(path) > 50:
                continue
            visited.add(current)

            node = self.nodes.get(current)
            if node and node.status in ("ERROR", "WARNING"):
                prio = node.priority
                if prio not in roots_by_prio or len(path) > len(roots_by_prio[prio][1]):
                    roots_by_prio[prio] = (current, list(path))

            for neighbor in self.forward_edges.get(current, set()):
                if neighbor not in visited and neighbor in self.nodes:
                    stack.append((neighbor, path + [neighbor]))

        if not roots_by_prio:
            return [(start_id, [start_id])]
        return list(roots_by_prio.values())

    def _reverse_bfs(self, root_id: str) -> Tuple[int, int, List[str]]:
        visited: Set[str] = {root_id}
        queue: deque = deque([(root_id, 0)])
        max_depth = 0
        affected: List[str] = [root_id]

        while queue:
            current, d = queue.popleft()
            for neighbor in self.reverse_edges.get(current, set()):
                if neighbor not in visited and neighbor in self.nodes:
                    visited.add(neighbor)
                    affected.append(neighbor)
                    new_depth = d + 1
                    if new_depth > max_depth:
                        max_depth = new_depth
                    queue.append((neighbor, new_depth))

        return len(affected), max_depth, affected

    def get_all_edges(self) -> List[Tuple[str, str]]:
        edges = []
        for from_id, targets in self.forward_edges.items():
            for to_id in targets:
                edges.append((from_id, to_id))
        return edges

    def get_error_subgraph(self) -> Tuple[List[GraphNode], List[Tuple[str, str]]]:
        error_ids = {
            nid for nid, n in self.nodes.items()
            if n.status in ("ERROR", "WARNING")
        }
        related_ids = set(error_ids)
        for eid in error_ids:
            related_ids.update(self.forward_edges.get(eid, set()))
            related_ids.update(self.reverse_edges.get(eid, set()))

        sub_nodes = [self.nodes[nid] for nid in related_ids if nid in self.nodes]
        sub_edges = [
            (f, t) for f, targets in self.forward_edges.items()
            for t in targets
            if f in related_ids and t in related_ids
        ]
        return sub_nodes, sub_edges

    def to_dict(self) -> dict:
        nodes_list = []
        for n in self.nodes.values():
            nodes_list.append({
                "id": n.id,
                "kind": n.kind,
                "name": n.name,
                "namespace": n.namespace,
                "status": n.status,
                "error_reason": n.error_reason,
                "error_message": n.error_message,
                "priority": n.priority,
                "is_system": n.is_system,
            })
        edges_list = self.get_all_edges()
        return {
            "nodes": nodes_list,
            "edges": [{"from": f, "to": t} for f, t in edges_list],
        }

    def get_error_summary_for_llm(self) -> str:
        lines = []
        for nid, node in self.nodes.items():
            if node.status in ("ERROR", "WARNING") and not node.is_system:
                deps = []
                for child_key in self.forward_edges.get(nid, []):
                    child = self.nodes.get(child_key)
                    if child:
                        deps.append(f"{child.kind}/{child.name}")
                dep_str = ", ".join(deps) if deps else "없음"
                lines.append(
                    f"- {node.kind}/{node.namespace}/{node.name} "
                    f"(상태: {node.status}, 원인: {node.error_reason}, "
                    f"메시지: {node.error_message}) "
                    f"→ 의존: [{dep_str}]"
                )
        if not lines:
            return "정상 상태 (이상 노드 없음)"
        return f"Kubernetes 클러스터 이상 노드 {len(lines)}개:\n" + "\n".join(lines)

    # ─── 신규 리소스 등록 ─────────────────────

    def _register_hpas(self, items: List[MiniHPA]) -> None:
        for item in items:
            nid = self._node_id("HPA", item.namespace, item.name)
            gnode = GraphNode(
                id=nid, kind="HPA", name=item.name,
                namespace=item.namespace, status="OK",
                error_reason="", error_message="", priority=5,
                is_system=(item.namespace in SYSTEM_NAMESPACES), raw=None
            )
            self._eval_hpa(gnode, item)
            self._add_node(gnode)

    def _register_jobs(self, items: List[MiniJob]) -> None:
        for item in items:
            nid = self._node_id("Job", item.namespace, item.name)
            gnode = GraphNode(
                id=nid, kind="Job", name=item.name,
                namespace=item.namespace, status="OK",
                error_reason="", error_message="", priority=4,
                is_system=(item.namespace in SYSTEM_NAMESPACES), raw=None
            )
            self._eval_job(gnode, item)
            self._add_node(gnode)

    def _register_cronjobs(self, items: List[MiniCronJob]) -> None:
        for item in items:
            nid = self._node_id("CronJob", item.namespace, item.name)
            gnode = GraphNode(
                id=nid, kind="CronJob", name=item.name,
                namespace=item.namespace, status="OK",
                error_reason="", error_message="", priority=4,
                is_system=(item.namespace in SYSTEM_NAMESPACES), raw=None
            )
            self._eval_cronjob(gnode, item)
            self._add_node(gnode)

    def _register_pvs(self, items: List[MiniPV]) -> None:
        for item in items:
            nid = self._node_id("PV", "", item.name)
            gnode = GraphNode(
                id=nid, kind="PV", name=item.name,
                namespace="", status="OK",
                error_reason="", error_message="", priority=3,
                is_system=False, raw=None
            )
            self._eval_pv(gnode, item)
            self._add_node(gnode)

    # ─── 신규 상태 평가 ───────────────────────

    def _eval_hpa(self, gnode: GraphNode, item: MiniHPA) -> None:
        if item.status == "ERROR":
            gnode.status = "ERROR"
            gnode.error_reason = item.error_reason
        elif item.status == "WARNING":
            gnode.status = "WARNING"
            gnode.error_reason = item.error_reason

    def _eval_job(self, gnode: GraphNode, item: MiniJob) -> None:
        if item.status == "ERROR":
            gnode.status = "ERROR"
            gnode.error_reason = item.error_reason
        elif item.status == "WARNING":
            gnode.status = "WARNING"
            gnode.error_reason = item.error_reason

    def _eval_cronjob(self, gnode: GraphNode, item: MiniCronJob) -> None:
        if item.status == "ERROR":
            gnode.status = "ERROR"
            gnode.error_reason = item.error_reason
        elif item.status == "WARNING":
            gnode.status = "WARNING"
            gnode.error_reason = item.error_reason

    def _eval_pv(self, gnode: GraphNode, item: MiniPV) -> None:
        if item.status == "Failed":
            gnode.status = "ERROR"
            gnode.error_reason = "PVFailed"
        elif item.status == "Released":
            gnode.status = "WARNING"
            gnode.error_reason = "PVReleased"

    # ─── 신규 엣지 링킹 ───────────────────────

    def _link_hpa_edges(self, hpas: List[MiniHPA]) -> None:
        """HPA → 스케일 대상 (Deployment/StatefulSet) 엣지"""
        for hpa in hpas:
            hpa_id = self._node_id("HPA", hpa.namespace, hpa.name)
            target_id = self._node_id(hpa.target_kind, hpa.namespace, hpa.target_name)
            if target_id in self.nodes:
                self._add_edge(target_id, hpa_id)  # Deployment → HPA 의존

    def _link_job_edges(self, pods: list) -> None:
        """Job → Pod (ownerReference로 연결)"""
        for pod in pods:
            if pod.owner and pod.owner[0] == "Job":
                pod_id = self._node_id("Pod", pod.namespace, pod.name)
                job_id = self._node_id("Job", pod.namespace, pod.owner[1])
                if job_id in self.nodes:
                    self._add_edge(pod_id, job_id)

    def _link_cronjob_edges(self, jobs: List[MiniJob]) -> None:
        """CronJob → Job (ownerReference로 연결)"""
        for job in jobs:
            if job.owner_name:
                job_id = self._node_id("Job", job.namespace, job.name)
                cj_id = self._node_id("CronJob", job.namespace, job.owner_name)
                if cj_id in self.nodes:
                    self._add_edge(job_id, cj_id)

    def _link_pv_edges(self, pvs: List[MiniPV]) -> None:
        """PV ↔ PVC 바인딩 엣지"""
        for pv in pvs:
            if pv.bound_pvc_name:
                pv_id = self._node_id("PV", "", pv.name)
                pvc_id = self._node_id("PVC", pv.bound_pvc_namespace, pv.bound_pvc_name)
                if pvc_id in self.nodes:
                    self._add_edge(pvc_id, pv_id)  # PVC → PV 의존

    # ─── 인프라 의존성 (조건부) ──────────────

    def _link_infra_dependencies(self, cache) -> None:
        """인프라 컴포넌트(CNI, DNS, kube-proxy)가 비정상일 때만 인과 엣지 추가"""
        for ds in cache.daemon_sets:
            for (ns, name_keyword), impact in INFRA_COMPONENTS.items():
                if ds.namespace == ns and name_keyword in ds.name:
                    ds_id = self._node_id("DaemonSet", ds.namespace, ds.name)
                    ds_node = self.nodes.get(ds_id)
                    # 조건부: DaemonSet이 비정상일 때만 엣지 추가
                    if ds_node and ds_node.status in ("ERROR", "WARNING"):
                        if impact == "all_nodes":
                            for node in cache.nodes:
                                node_id = self._node_id("Node", "", node.name)
                                if node_id in self.nodes:
                                    self._add_edge(node_id, ds_id)
                        elif impact == "all_pods":
                            for pod in cache.pods:
                                if pod.namespace not in SYSTEM_NAMESPACES:
                                    pod_id = self._node_id("Pod", pod.namespace, pod.name)
                                    if pod_id in self.nodes:
                                        self._add_edge(pod_id, ds_id)
                        elif impact == "all_services":
                            for svc in cache.services:
                                if svc.namespace not in SYSTEM_NAMESPACES:
                                    svc_id = self._node_id("Service", svc.namespace, svc.name)
                                    if svc_id in self.nodes:
                                        self._add_edge(svc_id, ds_id)
