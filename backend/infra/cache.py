"""
infra/cache.py
K8s API 17회 호출 → 경량 dataclass로 즉시 변환 → 메모리 캐싱
"""
from typing import Set, Tuple
from kubernetes import client

from infra.config import load_k8s_config
from infra.models import (
    MiniPod, MiniContainer, MiniDeployment, MiniReplicaSet,
    MiniStatefulSet, MiniDaemonSet, MiniService, MiniIngress,
    MiniPVC, MiniNode, MiniConfigMap, MiniSecret,
    MiniHPA, MiniJob, MiniCronJob, MiniPV,
)


class ResourceCache:

    def __init__(self):
        load_k8s_config()
        core = client.CoreV1Api()
        apps = client.AppsV1Api()
        net = client.NetworkingV1Api()
        storage = client.StorageV1Api()

        print("[INFO] K8s 리소스 캐싱 시작 (17회 API 호출)")

        success_count = 0
        fail_count = 0

        # Node
        try:
            nodes_raw = core.list_node().items
            self.nodes = [self._slim_node(n) for n in nodes_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Node 수집 실패: {e}")
            self.nodes = []
            fail_count += 1

        # Pod
        try:
            pods_raw = core.list_pod_for_all_namespaces().items
            self.pods = [self._slim_pod(p) for p in pods_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Pod 수집 실패: {e}")
            self.pods = []
            fail_count += 1

        # Deployment
        try:
            deploys_raw = apps.list_deployment_for_all_namespaces().items
            self.deployments = [self._slim_deploy(d) for d in deploys_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Deployment 수집 실패: {e}")
            self.deployments = []
            fail_count += 1

        # ReplicaSet
        try:
            rs_raw = apps.list_replica_set_for_all_namespaces().items
            self.replica_sets = [self._slim_rs(r) for r in rs_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] ReplicaSet 수집 실패: {e}")
            self.replica_sets = []
            fail_count += 1

        # StatefulSet
        try:
            sts_raw = apps.list_stateful_set_for_all_namespaces().items
            self.stateful_sets = [self._slim_sts(s) for s in sts_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] StatefulSet 수집 실패: {e}")
            self.stateful_sets = []
            fail_count += 1

        # DaemonSet
        try:
            ds_raw = apps.list_daemon_set_for_all_namespaces().items
            self.daemon_sets = [self._slim_ds(d) for d in ds_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] DaemonSet 수집 실패: {e}")
            self.daemon_sets = []
            fail_count += 1

        # Ingress
        try:
            ing_raw = net.list_ingress_for_all_namespaces().items
            self.ingresses = [self._slim_ingress(i) for i in ing_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Ingress 수집 실패: {e}")
            self.ingresses = []
            fail_count += 1

        # PVC
        try:
            pvc_raw = core.list_persistent_volume_claim_for_all_namespaces().items
            self.pvcs = [self._slim_pvc(p) for p in pvc_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] PVC 수집 실패: {e}")
            self.pvcs = []
            fail_count += 1

        # Service + Endpoints
        try:
            raw_endpoints = core.list_endpoints_for_all_namespaces().items
            ep_map = {(ep.metadata.namespace, ep.metadata.name): ep for ep in raw_endpoints}
            success_count += 1
        except Exception as e:
            print(f"[WARN] Endpoints 수집 실패: {e}")
            ep_map = {}
            fail_count += 1

        try:
            svc_raw = core.list_service_for_all_namespaces().items
            self.services = [self._slim_svc(s, ep_map) for s in svc_raw]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Service 수집 실패: {e}")
            self.services = []
            fail_count += 1

        # ConfigMap
        try:
            cm_raw = core.list_config_map_for_all_namespaces().items
            self.configmaps = [
                MiniConfigMap(name=cm.metadata.name, namespace=cm.metadata.namespace, key_count=len(cm.data or {}))
                for cm in cm_raw
            ]
            success_count += 1
        except Exception as e:
            print(f"[WARN] ConfigMap 수집 실패: {e}")
            self.configmaps = []
            fail_count += 1

        # Secret
        try:
            secret_raw = core.list_secret_for_all_namespaces().items
            self.secrets = [
                MiniSecret(name=s.metadata.name, namespace=s.metadata.namespace, secret_type=s.type or "", key_count=len(s.data or {}))
                for s in secret_raw
            ]
            success_count += 1
        except Exception as e:
            print(f"[WARN] Secret 수집 실패: {e}")
            self.secrets = []
            fail_count += 1

        # StorageClass
        try:
            self.storage_classes: Set[str] = {sc.metadata.name for sc in storage.list_storage_class().items}
            success_count += 1
        except Exception as e:
            print(f"[WARN] StorageClass 수집 실패: {e}")
            self.storage_classes: Set[str] = set()
            fail_count += 1

        # HPA
        try:
            autoscaling = client.AutoscalingV1Api()
            hpas_raw = autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces().items
            success_count += 1
        except Exception as e:
            print(f"[WARN] HPA 수집 실패: {e}")
            hpas_raw = []
            fail_count += 1

        # Job
        try:
            batch = client.BatchV1Api()
            jobs_raw = batch.list_job_for_all_namespaces().items
            success_count += 1
        except Exception as e:
            print(f"[WARN] Job 수집 실패: {e}")
            jobs_raw = []
            fail_count += 1

        # CronJob
        try:
            cronjobs_raw = batch.list_cron_job_for_all_namespaces().items
            success_count += 1
        except Exception as e:
            print(f"[WARN] CronJob 수집 실패: {e}")
            cronjobs_raw = []
            fail_count += 1

        # PV
        try:
            pvs_raw = core.list_persistent_volume().items
            success_count += 1
        except Exception as e:
            print(f"[WARN] PV 수집 실패: {e}")
            pvs_raw = []
            fail_count += 1

        self.hpas = [self._slim_hpa(h) for h in hpas_raw]
        self.jobs = [self._slim_job(j) for j in jobs_raw]
        self.cronjobs = [self._slim_cronjob(c) for c in cronjobs_raw]
        self.pvs = [self._slim_pv(p) for p in pvs_raw]

        # 빠른 존재 확인용 Set
        self.cm_exists: Set[Tuple[str, str]] = {(cm.namespace, cm.name) for cm in self.configmaps}
        self.secret_exists: Set[Tuple[str, str]] = {(s.namespace, s.name) for s in self.secrets}
        self.svc_exists: Set[Tuple[str, str]] = {(s.namespace, s.name) for s in self.services}

        # 일반 진단 API는 가능한 데이터로 계속 동작할 수 있지만, 실험 snapshot은
        # 부분 관측본을 정상 결과로 취급하면 안 됩니다. 호출자가 수집 무결성을
        # 명시적으로 검사할 수 있도록 최종 집계값을 객체에 보존합니다.
        self.collection_success_count = success_count
        self.collection_failure_count = fail_count
        self.collection_expected_count = 17

        print(f"[INFO] 수집 완료: 성공 {success_count}/17, 실패 {fail_count}/17")
        print(f"[INFO] Node:{len(self.nodes)} Pod:{len(self.pods)} "
              f"Deploy:{len(self.deployments)} SVC:{len(self.services)} "
              f"RS:{len(self.replica_sets)} STS:{len(self.stateful_sets)} "
              f"DS:{len(self.daemon_sets)} PVC:{len(self.pvcs)} "
              f"Ingress:{len(self.ingresses)} CM:{len(self.configmaps)} "
              f"Secret:{len(self.secrets)} SC:{len(self.storage_classes)}")
        print(f"  HPA:{len(self.hpas)}, Job:{len(self.jobs)}, CronJob:{len(self.cronjobs)}, PV:{len(self.pvs)}")

    # ── 변환 함수들 ─────────────────────────────────────────

    def _slim_pod(self, pod) -> MiniPod:
        meta = pod.metadata
        spec = pod.spec
        status = pod.status

        owner = None
        for ref in (meta.owner_references or []):
            if ref.kind in ("ReplicaSet", "StatefulSet", "DaemonSet", "Job"):
                owner = (ref.kind, ref.name)
                break

        all_specs = (spec.containers or []) + (spec.init_containers or [])
        all_statuses = list(status.container_statuses or []) + list(status.init_container_statuses or [])
        image_map = {c.name: c.image for c in all_specs}

        containers = []
        for cs in all_statuses:
            state = "running"
            reason = ""
            message = ""
            exit_code = 0

            if cs.state:
                if cs.state.waiting:
                    state = "waiting"
                    reason = cs.state.waiting.reason or ""
                    message = cs.state.waiting.message or ""
                elif cs.state.terminated:
                    state = "terminated"
                    reason = cs.state.terminated.reason or ""
                    exit_code = cs.state.terminated.exit_code or 0

            containers.append(MiniContainer(
                name=cs.name,
                image=image_map.get(cs.name, ""),
                state=state,
                reason=reason,
                message=message,
                exit_code=exit_code,
                restart_count=cs.restart_count or 0,
            ))

        pvc_names = []
        cm_names = []
        secret_names = []

        for vol in (spec.volumes or []):
            if vol.persistent_volume_claim:
                pvc_names.append(vol.persistent_volume_claim.claim_name)
            if vol.config_map:
                cm_names.append(vol.config_map.name)
            if vol.secret:
                secret_names.append(vol.secret.secret_name)

        for container_spec in all_specs:
            for env in (container_spec.env or []):
                if env.value_from:
                    if env.value_from.config_map_key_ref:
                        cm_names.append(env.value_from.config_map_key_ref.name)
                    if env.value_from.secret_key_ref:
                        secret_names.append(env.value_from.secret_key_ref.name)
            for env_from in (container_spec.env_from or []):
                if env_from.config_map_ref:
                    cm_names.append(env_from.config_map_ref.name)
                if env_from.secret_ref:
                    secret_names.append(env_from.secret_ref.name)

        pvc_names = list(set(pvc_names))
        cm_names = list(set(cm_names))
        secret_names = list(set(secret_names))

        pod_status = "OK"
        error_reason = ""
        phase = status.phase or "Unknown"

        if phase == "Failed":
            pod_status = "ERROR"
            error_reason = status.reason or "Failed"
        elif phase == "Pending":
            pod_status = "WARNING"
            error_reason = "Pending"

        for c in containers:
            if c.state == "waiting" and c.reason in (
                "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                "CreateContainerConfigError", "InvalidImageName",
                "CreateContainerError", "RunContainerError"
            ):
                pod_status = "ERROR"
                error_reason = c.reason
                break
            if c.state == "terminated" and c.exit_code != 0:
                pod_status = "ERROR"
                error_reason = c.reason or "NonZeroExit"
                break
            if c.restart_count >= 5 and pod_status == "OK":
                pod_status = "WARNING"
                error_reason = "HighRestartCount"

        return MiniPod(
            name=meta.name, namespace=meta.namespace,
            phase=phase, node_name=spec.node_name or "",
            owner=owner, containers=containers,
            pvc_names=pvc_names, cm_names=cm_names, secret_names=secret_names,
            status=pod_status, error_reason=error_reason,
        )

    def _slim_deploy(self, deploy) -> MiniDeployment:
        desired = deploy.spec.replicas or 1
        available = deploy.status.available_replicas or 0
        ready = deploy.status.ready_replicas or 0
        status = "OK"
        error_reason = ""
        if available == 0 and desired > 0:
            status = "ERROR"
            error_reason = "NoAvailableReplicas"
        elif ready < desired:
            status = "WARNING"
            error_reason = "ReplicasMismatch"
        return MiniDeployment(
            name=deploy.metadata.name, namespace=deploy.metadata.namespace,
            desired=desired, available=available, ready=ready,
            status=status, error_reason=error_reason,
        )

    def _slim_rs(self, rs) -> MiniReplicaSet:
        owner_name = ""
        for ref in (rs.metadata.owner_references or []):
            if ref.kind == "Deployment":
                owner_name = ref.name
                break
        return MiniReplicaSet(
            name=rs.metadata.name, namespace=rs.metadata.namespace,
            desired=rs.spec.replicas or 0, ready=rs.status.ready_replicas or 0,
            owner_name=owner_name,
        )

    def _slim_sts(self, sts) -> MiniStatefulSet:
        return MiniStatefulSet(
            name=sts.metadata.name, namespace=sts.metadata.namespace,
            desired=sts.spec.replicas or 1, ready=sts.status.ready_replicas or 0,
        )

    def _slim_ds(self, ds) -> MiniDaemonSet:
        return MiniDaemonSet(
            name=ds.metadata.name, namespace=ds.metadata.namespace,
            desired=ds.status.desired_number_scheduled or 0,
            ready=ds.status.number_ready or 0,
        )

    def _slim_svc(self, svc, ep_map: dict) -> MiniService:
        ns = svc.metadata.namespace
        name = svc.metadata.name
        ep = ep_map.get((ns, name))
        endpoint_addrs = []
        has_ready = False
        if ep:
            for subset in (ep.subsets or []):
                if subset.addresses:
                    has_ready = True
                    for addr in subset.addresses[:5]:
                        for port in (subset.ports or []):
                            endpoint_addrs.append(f"{addr.ip}:{port.port}")
        return MiniService(
            name=name, namespace=ns,
            selector=dict(svc.spec.selector or {}),
            cluster_ip=svc.spec.cluster_ip or "",
            svc_type=svc.spec.type or "ClusterIP",
            has_ready_endpoints=has_ready,
            endpoint_addrs=endpoint_addrs[:5],
        )

    def _slim_ingress(self, ing) -> MiniIngress:
        backend_services = []
        hosts = []
        tls_secrets = []
        for rule in (ing.spec.rules or []):
            if rule.host:
                hosts.append(rule.host)
            if rule.http:
                for path in (rule.http.paths or []):
                    if path.backend and path.backend.service:
                        backend_services.append(path.backend.service.name)
        for tls in (ing.spec.tls or []):
            if tls.secret_name:
                tls_secrets.append(tls.secret_name)
        return MiniIngress(
            name=ing.metadata.name, namespace=ing.metadata.namespace,
            backend_services=list(set(backend_services)),
            tls_secrets=list(set(tls_secrets)), hosts=hosts,
        )

    def _slim_pvc(self, pvc) -> MiniPVC:
        size = ""
        if pvc.spec.resources and pvc.spec.resources.requests:
            size = pvc.spec.resources.requests.get("storage", "")
        return MiniPVC(
            name=pvc.metadata.name, namespace=pvc.metadata.namespace,
            phase=pvc.status.phase or "Unknown",
            storage_class=pvc.spec.storage_class_name or "", size=size,
        )

    def _slim_node(self, node) -> MiniNode:
        conditions = node.status.conditions or []
        ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
        pressures = [
            c.type for c in conditions
            if c.type in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.status == "True"
        ]
        labels = node.metadata.labels or {}
        role = "control-plane" if "node-role.kubernetes.io/control-plane" in labels else "worker"
        addrs = node.status.addresses or []
        ip = next((a.address for a in addrs if a.type == "InternalIP"), "")
        alloc = node.status.allocatable or {}
        return MiniNode(
            name=node.metadata.name, ready=ready, pressures=pressures,
            ip=ip, role=role, cpu=alloc.get("cpu", ""), memory=alloc.get("memory", ""),
        )

    def _slim_hpa(self, hpa) -> MiniHPA:
        spec = hpa.spec
        status = hpa.status
        s = "OK"
        reason = ""
        if (status.current_replicas or 0) != (status.desired_number_of_replicas or 0):
            s = "WARNING"
            reason = "ScalingInProgress"
        if hpa.status.conditions:
            for cond in hpa.status.conditions:
                if cond.type == "ScalingLimited" and cond.status == "True":
                    s = "WARNING"
                    reason = "ScalingLimited"
                if cond.type == "AbleToScale" and cond.status == "False":
                    s = "ERROR"
                    reason = "FailedGetMetrics"
        return MiniHPA(
            name=hpa.metadata.name,
            namespace=hpa.metadata.namespace,
            target_kind=spec.scale_target_ref.kind,
            target_name=spec.scale_target_ref.name,
            min_replicas=spec.min_replicas or 1,
            max_replicas=spec.max_replicas,
            current_replicas=status.current_replicas or 0,
            desired_replicas=status.desired_number_of_replicas or 0,
            status=s,
            error_reason=reason,
        )

    def _slim_job(self, job) -> MiniJob:
        spec = job.spec
        status = job.status
        succeeded = status.succeeded or 0
        failed = status.failed or 0
        completions = spec.completions or 1
        s = "OK"
        reason = ""
        if failed > 0:
            s = "ERROR"
            reason = "BackoffLimitExceeded"
            if spec.active_deadline_seconds and status.start_time:
                reason = "DeadlineExceeded"
        elif succeeded < completions and not status.active:
            s = "WARNING"
            reason = "Incomplete"
        owner_name = ""
        if job.metadata.owner_references:
            for ref in job.metadata.owner_references:
                if ref.kind == "CronJob":
                    owner_name = ref.name
                    break
        return MiniJob(
            name=job.metadata.name,
            namespace=job.metadata.namespace,
            completions=completions,
            succeeded=succeeded,
            failed=failed,
            owner_name=owner_name,
            status=s,
            error_reason=reason,
        )

    def _slim_cronjob(self, cj) -> MiniCronJob:
        spec = cj.spec
        status = cj.status
        last = ""
        if status.last_schedule_time:
            last = status.last_schedule_time.isoformat()
        active_count = len(status.active) if status.active else 0
        s = "OK"
        reason = ""
        if not status.last_schedule_time and cj.metadata.creation_timestamp:
            s = "WARNING"
            reason = "NeverScheduled"
        return MiniCronJob(
            name=cj.metadata.name,
            namespace=cj.metadata.namespace,
            schedule=spec.schedule,
            last_schedule_time=last,
            active_count=active_count,
            status=s,
            error_reason=reason,
        )

    def _slim_pv(self, pv) -> MiniPV:
        spec = pv.spec
        status_phase = pv.status.phase if pv.status else "Unknown"
        bound_pvc_name = ""
        bound_pvc_ns = ""
        if spec.claim_ref:
            bound_pvc_name = spec.claim_ref.name or ""
            bound_pvc_ns = spec.claim_ref.namespace or ""
        capacity = ""
        if spec.capacity and "storage" in spec.capacity:
            capacity = spec.capacity["storage"]
        return MiniPV(
            name=pv.metadata.name,
            capacity=capacity,
            access_modes=spec.access_modes or [],
            reclaim_policy=spec.persistent_volume_reclaim_policy or "",
            status=status_phase,
            bound_pvc_name=bound_pvc_name,
            bound_pvc_namespace=bound_pvc_ns,
            storage_class=spec.storage_class_name or "",
        )
