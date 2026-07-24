"""
analysis/analyzers.py
Kubernetes 리소스 이상 상태 탐지 — 전체 Analyzer 통합
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

from kubernetes import client

from infra.config import load_k8s_config
from infra.models import MiniPod

if TYPE_CHECKING:
    from infra.cache import ResourceCache


# ── Failure 데이터클래스 ─────────────────────────────────────

@dataclass
class PodFailure:
    pod_name: str
    namespace: str
    reason: str
    message: str
    container_name: str = ""
    exit_code: int = 0
    events: List[str] = field(default_factory=list)

@dataclass
class DeploymentFailure:
    name: str
    namespace: str
    reason: str
    message: str

@dataclass
class NodeFailure:
    node_name: str
    reason: str
    message: str

@dataclass
class ServiceFailure:
    name: str
    namespace: str
    reason: str
    message: str

@dataclass
class IngressFailure:
    name: str
    namespace: str
    reason: str
    message: str

@dataclass
class PVCFailure:
    name: str
    namespace: str
    reason: str
    message: str


@dataclass
class HPAFailure:
    name: str
    namespace: str
    reason: str       # "ScalingLimited", "FailedGetMetrics"
    message: str


@dataclass
class JobFailure:
    name: str
    namespace: str
    reason: str       # "BackoffLimitExceeded", "DeadlineExceeded"
    message: str


@dataclass
class CronJobFailure:
    name: str
    namespace: str
    reason: str       # "NeverScheduled", "MissedSchedule"
    message: str


@dataclass
class PVFailure:
    name: str
    reason: str       # "PVFailed", "PVReleased"
    message: str


# ── Analyzer 클래스 ──────────────────────────────────────────

class PodAnalyzer:
    """Pod 상태 기반 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[PodFailure]:
        failures = []
        for pod in cache.pods:
            failures.extend(self._check_pod(pod))
        return failures

    def analyze_live(self, namespace: str = "") -> List[PodFailure]:
        load_k8s_config()
        v1 = client.CoreV1Api()
        if namespace:
            pods = v1.list_namespaced_pod(namespace).items
        else:
            pods = v1.list_pod_for_all_namespaces().items
        failures = []
        for pod in pods:
            failures.extend(self._analyze_pod_live(pod, v1))
        return failures

    def _analyze_pod_live(self, pod, v1) -> List[PodFailure]:
        failures = []
        name = pod.metadata.name
        ns = pod.metadata.namespace
        phase = pod.status.phase or ""

        if phase == "Pending":
            for condition in (pod.status.conditions or []):
                if (condition.type == "PodScheduled"
                        and condition.reason == "Unschedulable"
                        and condition.message):
                    failures.append(PodFailure(
                        pod_name=name, namespace=ns,
                        reason="Unschedulable", message=condition.message,
                    ))

        all_statuses = list(pod.status.init_container_statuses or []) + \
                       list(pod.status.container_statuses or [])

        for cs in all_statuses:
            if cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                msg = cs.state.waiting.message or ""

                if reason == "CrashLoopBackOff" and cs.last_state.terminated:
                    failures.append(PodFailure(
                        pod_name=name, namespace=ns,
                        reason="CrashLoopBackOff",
                        message=f"마지막 종료 이유: {cs.last_state.terminated.reason} (컨테이너: {cs.name})",
                        container_name=cs.name,
                        exit_code=cs.last_state.terminated.exit_code or 0,
                    ))
                elif reason in [
                    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                    "InvalidImageName", "CreateContainerConfigError",
                    "CreateContainerError", "RunContainerError",
                    "PreStartHookError", "OOMKilled", "Evicted"
                ] and msg:
                    failures.append(PodFailure(
                        pod_name=name, namespace=ns,
                        reason=reason, message=msg,
                        container_name=cs.name,
                    ))

            elif cs.state.terminated:
                exit_code = cs.state.terminated.exit_code or 0
                if exit_code != 0:
                    term_reason = cs.state.terminated.reason or "Unknown"
                    failures.append(PodFailure(
                        pod_name=name, namespace=ns,
                        reason=term_reason,
                        message=f"비정상 종료 (ExitCode={exit_code}, 컨테이너: {cs.name})",
                        container_name=cs.name, exit_code=exit_code,
                    ))
            else:
                if not cs.ready and phase == "Running":
                    try:
                        events = v1.list_namespaced_event(
                            ns, field_selector=f"involvedObject.name={name}"
                        )
                        event_msgs = [e.message for e in events.items if e.message]
                    except Exception:
                        event_msgs = []
                    unhealthy = [e for e in event_msgs if "Unhealthy" in e]
                    if unhealthy:
                        failures.append(PodFailure(
                            pod_name=name, namespace=ns,
                            reason="ReadinessProbe Failure",
                            message=unhealthy[0],
                            container_name=cs.name, events=unhealthy,
                        ))

        return failures

    def _check_pod(self, pod: MiniPod) -> List[PodFailure]:
        """MiniPod 구조체 기반 분석"""
        failures = []

        for c in pod.containers:
            if c.state == "waiting" and c.reason == "CrashLoopBackOff":
                failures.append(PodFailure(
                    pod_name=pod.name, namespace=pod.namespace,
                    reason="CrashLoopBackOff",
                    message=f"컨테이너 '{c.name}' 재시작 반복",
                    container_name=c.name, exit_code=c.exit_code,
                ))
            elif c.state == "waiting" and c.reason in (
                "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                "CreateContainerConfigError", "CreateContainerError", "RunContainerError"
            ):
                failures.append(PodFailure(
                    pod_name=pod.name, namespace=pod.namespace,
                    reason=c.reason, message=c.message,
                    container_name=c.name,
                ))
            elif c.state == "terminated" and c.exit_code != 0:
                failures.append(PodFailure(
                    pod_name=pod.name, namespace=pod.namespace,
                    reason=c.reason or "NonZeroExit",
                    message=f"비정상 종료 (ExitCode={c.exit_code}, 컨테이너: {c.name})",
                    container_name=c.name, exit_code=c.exit_code,
                ))

        return failures


class DeploymentAnalyzer:
    """Deployment 복제본 수 기반 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[DeploymentFailure]:
        failures = []
        for deploy in cache.deployments:
            if deploy.available == 0 and deploy.desired > 0:
                failures.append(DeploymentFailure(
                    name=deploy.name, namespace=deploy.namespace,
                    reason="NoAvailableReplicas",
                    message=f"원하는 복제본 수: {deploy.desired}, 사용 가능: 0",
                ))
            elif deploy.ready < deploy.desired:
                failures.append(DeploymentFailure(
                    name=deploy.name, namespace=deploy.namespace,
                    reason="InsufficientReplicas",
                    message=f"원하는 복제본: {deploy.desired}, 준비된 복제본: {deploy.ready}",
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[DeploymentFailure]:
        load_k8s_config()
        apps_v1 = client.AppsV1Api()
        if namespace:
            deploys = apps_v1.list_namespaced_deployment(namespace).items
        else:
            deploys = apps_v1.list_deployment_for_all_namespaces().items
        failures = []
        for deploy in deploys:
            desired = deploy.spec.replicas or 0
            available = deploy.status.available_replicas or 0
            ready = deploy.status.ready_replicas or 0
            if available == 0 and desired > 0:
                failures.append(DeploymentFailure(
                    name=deploy.metadata.name, namespace=deploy.metadata.namespace,
                    reason="NoAvailableReplicas",
                    message=f"원하는 복제본 수: {desired}, 사용 가능: 0",
                ))
            elif ready < desired:
                failures.append(DeploymentFailure(
                    name=deploy.metadata.name, namespace=deploy.metadata.namespace,
                    reason="InsufficientReplicas",
                    message=f"원하는 복제본: {desired}, 준비된 복제본: {ready}",
                ))
        return failures


class NodeAnalyzer:
    """노드 상태 기반 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[NodeFailure]:
        failures = []
        for node in cache.nodes:
            if not node.ready:
                failures.append(NodeFailure(
                    node_name=node.name, reason="NodeNotReady",
                    message="노드가 Ready 상태가 아닙니다.",
                ))
            for pressure in node.pressures:
                failures.append(NodeFailure(
                    node_name=node.name, reason=pressure,
                    message=f"{pressure} 상태가 감지되었습니다.",
                ))
        return failures

    def analyze_live(self) -> List[NodeFailure]:
        load_k8s_config()
        v1 = client.CoreV1Api()
        nodes = v1.list_node().items
        failures = []
        for node in nodes:
            for cond in (node.status.conditions or []):
                if cond.type == "Ready" and cond.status != "True":
                    failures.append(NodeFailure(
                        node_name=node.metadata.name, reason="NodeNotReady",
                        message=cond.message or "노드가 Ready 상태가 아닙니다.",
                    ))
                elif cond.type in ("MemoryPressure", "DiskPressure", "PIDPressure") and cond.status == "True":
                    failures.append(NodeFailure(
                        node_name=node.metadata.name, reason=cond.type,
                        message=cond.message or f"{cond.type} 상태가 감지되었습니다.",
                    ))
        return failures


class ServiceAnalyzer:
    """Service 엔드포인트 기반 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[ServiceFailure]:
        failures = []
        for svc in cache.services:
            if svc.namespace == "kube-system":
                continue
            if svc.cluster_ip == "None":
                continue
            if not svc.has_ready_endpoints:
                failures.append(ServiceFailure(
                    name=svc.name, namespace=svc.namespace,
                    reason="NoEndpoints",
                    message=f"Service '{svc.name}'에 연결된 Ready 상태의 Pod이 없습니다.",
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[ServiceFailure]:
        load_k8s_config()
        v1 = client.CoreV1Api()
        if namespace:
            services = v1.list_namespaced_service(namespace).items
        else:
            services = v1.list_service_for_all_namespaces().items
        failures = []
        for svc in services:
            ns = svc.metadata.namespace
            name = svc.metadata.name
            if ns == "kube-system":
                continue
            if svc.spec.cluster_ip == "None":
                continue
            try:
                ep = v1.read_namespaced_endpoints(name, ns)
                has_ready = any(
                    (subset.addresses and len(subset.addresses) > 0)
                    for subset in (ep.subsets or [])
                )
                if not has_ready:
                    failures.append(ServiceFailure(
                        name=name, namespace=ns, reason="NoEndpoints",
                        message=f"Service '{name}'에 연결된 Ready 상태의 Pod이 없습니다.",
                    ))
            except Exception:
                pass
        return failures


class IngressAnalyzer:
    """Ingress 백엔드 서비스/TLS Secret 참조 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[IngressFailure]:
        failures = []
        for ing in cache.ingresses:
            for svc_name in ing.backend_services:
                if (ing.namespace, svc_name) not in cache.svc_exists:
                    failures.append(IngressFailure(
                        name=ing.name, namespace=ing.namespace,
                        reason="MissingService",
                        message=f"Ingress '{ing.name}'이 참조하는 Service '{svc_name}'이 존재하지 않습니다.",
                    ))
            for secret_name in ing.tls_secrets:
                if (ing.namespace, secret_name) not in cache.secret_exists:
                    failures.append(IngressFailure(
                        name=ing.name, namespace=ing.namespace,
                        reason="MissingTLSSecret",
                        message=f"Ingress '{ing.name}'의 TLS Secret '{secret_name}'이 존재하지 않습니다.",
                    ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[IngressFailure]:
        load_k8s_config()
        net_v1 = client.NetworkingV1Api()
        v1 = client.CoreV1Api()
        if namespace:
            ingresses = net_v1.list_namespaced_ingress(namespace).items
        else:
            ingresses = net_v1.list_ingress_for_all_namespaces().items
        failures = []
        for ing in ingresses:
            ns = ing.metadata.namespace
            name = ing.metadata.name
            for rule in (ing.spec.rules or []):
                if not rule.http:
                    continue
                for path in (rule.http.paths or []):
                    svc_name = path.backend.service.name if path.backend.service else None
                    if svc_name:
                        try:
                            v1.read_namespaced_service(svc_name, ns)
                        except client.exceptions.ApiException:
                            failures.append(IngressFailure(
                                name=name, namespace=ns, reason="MissingService",
                                message=f"Ingress '{name}'이 참조하는 Service '{svc_name}'이 존재하지 않습니다.",
                            ))
            for tls in (ing.spec.tls or []):
                if tls.secret_name:
                    try:
                        v1.read_namespaced_secret(tls.secret_name, ns)
                    except client.exceptions.ApiException:
                        failures.append(IngressFailure(
                            name=name, namespace=ns, reason="MissingTLSSecret",
                            message=f"Ingress '{name}'의 TLS Secret '{tls.secret_name}'이 존재하지 않습니다.",
                        ))
        return failures


class PVCAnalyzer:
    """PVC 상태 기반 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[PVCFailure]:
        failures = []
        for pvc in cache.pvcs:
            if pvc.phase == "Pending":
                failures.append(PVCFailure(
                    name=pvc.name, namespace=pvc.namespace,
                    reason="PVCPending",
                    message=f"PVC '{pvc.name}'이 Pending 상태입니다.",
                ))
            elif pvc.phase == "Lost":
                failures.append(PVCFailure(
                    name=pvc.name, namespace=pvc.namespace,
                    reason="PVCLost",
                    message=f"PVC '{pvc.name}'이 Lost 상태입니다.",
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[PVCFailure]:
        load_k8s_config()
        v1 = client.CoreV1Api()
        if namespace:
            pvcs = v1.list_namespaced_persistent_volume_claim(namespace).items
        else:
            pvcs = v1.list_persistent_volume_claim_for_all_namespaces().items
        failures = []
        for pvc in pvcs:
            phase = pvc.status.phase
            if phase == "Pending":
                failures.append(PVCFailure(
                    name=pvc.metadata.name, namespace=pvc.metadata.namespace,
                    reason="PVCPending",
                    message=f"PVC '{pvc.metadata.name}'이 Pending 상태입니다.",
                ))
            elif phase == "Lost":
                failures.append(PVCFailure(
                    name=pvc.metadata.name, namespace=pvc.metadata.namespace,
                    reason="PVCLost",
                    message=f"PVC '{pvc.metadata.name}'이 Lost 상태입니다.",
                ))
        return failures


class HPAAnalyzer:
    """HPA 스케일링 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[HPAFailure]:
        failures = []
        for hpa in cache.hpas:
            if hpa.status in ("ERROR", "WARNING"):
                failures.append(HPAFailure(
                    name=hpa.name,
                    namespace=hpa.namespace,
                    reason=hpa.error_reason,
                    message=(
                        f"HPA {hpa.name}: current={hpa.current_replicas}, "
                        f"desired={hpa.desired_replicas}, "
                        f"target={hpa.target_kind}/{hpa.target_name}"
                    ),
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[HPAFailure]:
        return []  # 추후 구현


class JobAnalyzer:
    """Job/배치 작업 실패 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[JobFailure]:
        failures = []
        for job in cache.jobs:
            if job.status in ("ERROR", "WARNING"):
                failures.append(JobFailure(
                    name=job.name,
                    namespace=job.namespace,
                    reason=job.error_reason,
                    message=(
                        f"Job {job.name}: succeeded={job.succeeded}/{job.completions}, "
                        f"failed={job.failed}"
                    ),
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[JobFailure]:
        return []  # 추후 구현


class CronJobAnalyzer:
    """CronJob 스케줄 장애 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[CronJobFailure]:
        failures = []
        for cj in cache.cronjobs:
            if cj.status in ("ERROR", "WARNING"):
                failures.append(CronJobFailure(
                    name=cj.name,
                    namespace=cj.namespace,
                    reason=cj.error_reason,
                    message=(
                        f"CronJob {cj.name}: schedule={cj.schedule}, "
                        f"last_run={cj.last_schedule_time or 'Never'}"
                    ),
                ))
        return failures

    def analyze_live(self, namespace: str = "") -> List[CronJobFailure]:
        return []  # 추후 구현


class PVAnalyzer:
    """PersistentVolume 상태 탐지기"""

    def analyze_from_cache(self, cache: "ResourceCache") -> List[PVFailure]:
        failures = []
        for pv in cache.pvs:
            if pv.status in ("Failed", "Released"):
                failures.append(PVFailure(
                    name=pv.name,
                    reason="PVFailed" if pv.status == "Failed" else "PVReleased",
                    message=(
                        f"PV {pv.name}: status={pv.status}, capacity={pv.capacity}, "
                        f"bound_to={pv.bound_pvc_namespace}/{pv.bound_pvc_name}"
                    ),
                ))
        return failures

    def analyze_live(self) -> List[PVFailure]:
        return []  # 추후 구현
