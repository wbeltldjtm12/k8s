"""
infra/models.py
RCA + Tree 출력에 필요한 최소 정보만 담는 경량 데이터 모델
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


@dataclass(slots=True)
class MiniContainer:
    name: str
    image: str
    state: str          # "running" | "waiting" | "terminated"
    reason: str
    message: str
    exit_code: int
    restart_count: int


@dataclass(slots=True)
class MiniPod:
    name: str
    namespace: str
    phase: str
    node_name: str
    owner: Optional[Tuple[str, str]]   # (kind, name) 예: ("ReplicaSet", "nginx-abc")
    containers: List[MiniContainer]
    pvc_names: List[str]
    cm_names: List[str]
    secret_names: List[str]
    status: str = "OK"
    error_reason: str = ""


@dataclass(slots=True)
class MiniDeployment:
    name: str
    namespace: str
    desired: int
    available: int
    ready: int
    status: str = "OK"
    error_reason: str = ""


@dataclass(slots=True)
class MiniReplicaSet:
    name: str
    namespace: str
    desired: int
    ready: int
    owner_name: str     # Deployment 이름, 없으면 ""


@dataclass(slots=True)
class MiniStatefulSet:
    name: str
    namespace: str
    desired: int
    ready: int


@dataclass(slots=True)
class MiniDaemonSet:
    name: str
    namespace: str
    desired: int
    ready: int


@dataclass(slots=True)
class MiniService:
    name: str
    namespace: str
    selector: Dict[str, str]
    cluster_ip: str
    svc_type: str
    has_ready_endpoints: bool
    endpoint_addrs: List[str] = field(default_factory=list)


@dataclass(slots=True)
class MiniIngress:
    name: str
    namespace: str
    backend_services: List[str] = field(default_factory=list)
    tls_secrets: List[str] = field(default_factory=list)
    hosts: List[str] = field(default_factory=list)


@dataclass(slots=True)
class MiniPVC:
    name: str
    namespace: str
    phase: str
    storage_class: str
    size: str


@dataclass(slots=True)
class MiniNode:
    name: str
    ready: bool
    pressures: List[str] = field(default_factory=list)
    ip: str = ""
    role: str = ""
    cpu: str = ""
    memory: str = ""


@dataclass(slots=True, frozen=True)
class MiniConfigMap:
    name: str
    namespace: str
    key_count: int


@dataclass(slots=True, frozen=True)
class MiniSecret:
    name: str
    namespace: str
    secret_type: str
    key_count: int


@dataclass(slots=True)
class MiniHPA:
    name: str
    namespace: str
    target_kind: str          # "Deployment" 또는 "StatefulSet"
    target_name: str          # 스케일 대상 이름
    min_replicas: int
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    status: str               # "OK", "WARNING", "ERROR"
    error_reason: str         # "ScalingLimited", "FailedGetMetrics" 등


@dataclass(slots=True)
class MiniJob:
    name: str
    namespace: str
    completions: int          # spec.completions (목표 완료 수)
    succeeded: int            # status.succeeded
    failed: int               # status.failed
    owner_name: str           # CronJob 이름 (없으면 "")
    status: str               # "OK", "WARNING", "ERROR"
    error_reason: str         # "BackoffLimitExceeded", "DeadlineExceeded" 등


@dataclass(slots=True)
class MiniCronJob:
    name: str
    namespace: str
    schedule: str             # cron 표현식 "*/5 * * * *"
    last_schedule_time: str   # ISO 형식 또는 ""
    active_count: int         # 현재 실행 중인 Job 수
    status: str               # "OK", "WARNING", "ERROR"
    error_reason: str         # "MissedSchedule" 등


@dataclass(slots=True)
class MiniPV:
    name: str
    capacity: str             # "10Gi"
    access_modes: list        # ["ReadWriteOnce"]
    reclaim_policy: str       # "Retain", "Delete"
    status: str               # "Available", "Bound", "Released", "Failed"
    bound_pvc_name: str       # 바인딩된 PVC 이름 (없으면 "")
    bound_pvc_namespace: str  # 바인딩된 PVC 네임스페이스 (없으면 "")
    storage_class: str        # StorageClass 이름
