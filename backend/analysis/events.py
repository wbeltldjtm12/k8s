"""
analysis/events.py
====================
쿠버네티스 Warning 이벤트를 시간순으로 분석하여,
장애의 근본 원인(Root Cause)과 전파 체인(Causality Chain)을 추적합니다.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from kubernetes import client

from infra.config import load_k8s_config


# ── 에러 이유(reason) 키워드 → 에러 유형 매핑 ─────────────────────────
REASON_CATEGORY_MAP = {
    # Pod 관련
    "OOMKilling":           "메모리 초과 (OOM)",
    "OOMKilled":            "메모리 초과 (OOM)",
    "BackOff":              "컨테이너 재시작 반복 (CrashLoopBackOff)",
    "Failed":               "시작 실패",
    "FailedCreate":         "생성 실패",
    "FailedScheduling":     "스케줄링 실패 (노드 자원 부족)",
    "Preempting":           "우선순위에 의해 퇴거 예정",
    "Evicted":              "노드 자원 부족으로 퇴거됨",
    "ImagePullBackOff":     "이미지 풀 실패",
    "ErrImagePull":         "이미지 풀 실패",

    # PVC / Storage 관련
    "FailedMount":          "볼륨 마운트 실패 (PVC/StorageClass 문제)",
    "FailedAttachVolume":   "볼륨 연결 실패",
    "VolumeNotFound":       "볼륨 없음",
    "ProvisioningFailed":   "StorageClass Provisioner 실패",

    # Node 관련
    "NodeNotReady":         "노드 준비되지 않음",
    "NodeHasDiskPressure":  "노드 디스크 압박",
    "NodeHasMemoryPressure":"노드 메모리 압박",
    "NodeHasPIDPressure":   "노드 프로세스 압박",

    # Service / Network 관련
    "FailedToUpdateEndpoint": "서비스 엔드포인트 업데이트 실패",
    "DNSConfigForming":       "DNS 설정 생성 중",

    # Deployment / ReplicaSet 관련
    "ScalingReplicaSet":      "레플리카셋 스케일링",
    "DeploymentRollback":     "Deployment 롤백",
}


@dataclass
class K8sEvent:
    """정규화된 K8s 이벤트 객체"""
    timestamp: Optional[datetime]
    reason: str
    message: str
    kind: str           # 관련 리소스 타입 (Pod, Node, PVC...)
    name: str           # 리소스 이름
    namespace: str
    category: str       # 에러 유형 (한국어)
    count: int = 1      # 동일 이벤트 발생 횟수


@dataclass
class CausalityChain:
    """Root Cause부터 말단 증상까지의 인과관계 체인"""
    root_cause: K8sEvent
    chain: List[K8sEvent] = field(default_factory=list)
    summary: str = ""   # LLM에 전달할 요약 텍스트

    def build_summary(self) -> str:
        """인과관계 체인을 LLM이 이해하기 쉬운 텍스트로 요약"""
        if not self.chain:
            return (
                f"[Root Cause] {self.root_cause.kind}/{self.root_cause.name}: "
                f"{self.root_cause.category} — {self.root_cause.message}"
            )

        parts = [
            f"[Root Cause] {self.root_cause.kind}/{self.root_cause.name}: "
            f"{self.root_cause.category}"
        ]
        for i, ev in enumerate(self.chain, 1):
            parts.append(
                f"  └→ [{i}] {ev.kind}/{ev.name}: {ev.category}"
            )
        return "\n".join(parts)


class EventAnalyzer:
    """K8s 이벤트 시계열 분석기"""

    def __init__(self):
        load_k8s_config()
        self.v1 = client.CoreV1Api()

    # ── 이벤트 수집 ─────────────────────────────────────────────────────
    def _fetch_warning_events(self, since_minutes: int = 15) -> List[K8sEvent]:
        """
        전체 네임스페이스에서 Warning 이벤트를 수집하고 since_minutes 이후 필터링 후 시간순 정렬.
        since_minutes: 현재 시점 기준 몇 분 이내 이벤트만 수집할지 (기본 15분)
        """
        try:
            since_time = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
            raw_events = self.v1.list_event_for_all_namespaces(
                field_selector="type=Warning"
            ).items

            # 시간 필터: since_time 이후 이벤트만 포함
            filtered = []
            for e in raw_events:
                event_time = e.last_timestamp or e.event_time
                if event_time is None:
                    continue
                # timezone-aware 비교를 위해 tzinfo 보정
                if hasattr(event_time, "tzinfo") and event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=timezone.utc)
                if event_time >= since_time:
                    filtered.append(e)

        except Exception as e:
            print(f"[WARN] 이벤트 수집 실패: {e}")
            return []

        events: List[K8sEvent] = []
        for e in filtered:
            ts = e.last_timestamp or e.event_time
            reason = e.reason or "Unknown"
            category = REASON_CATEGORY_MAP.get(reason, f"기타 ({reason})")

            events.append(K8sEvent(
                timestamp=ts,
                reason=reason,
                message=e.message or "",
                kind=e.involved_object.kind or "Unknown",
                name=e.involved_object.name or "Unknown",
                namespace=e.involved_object.namespace or "cluster-level",
                category=category,
                count=e.count or 1,
            ))

        # 오래된 이벤트(=Root Cause 후보)가 앞으로 오도록 오름차순 정렬
        events.sort(key=lambda ev: ev.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return events

    # ── 인과관계 체인 구성 ───────────────────────────────────────────────
    def _build_chains(self, events: List[K8sEvent]) -> List[CausalityChain]:
        if not events:
            return []

        chains = []
        visited = set()

        for event in events:
            key = (event.name, event.namespace)
            if key in visited:
                continue

            visited.add(key)

            related = [
                e for e in events
                if e != event
                and e.namespace == event.namespace
                and (
                    e.name == event.name
                    or (len(event.name) >= 5 and event.name in e.message)
                )
            ]

            chain = CausalityChain(root_cause=event, chain=related)
            chain.summary = chain.build_summary()
            chains.append(chain)

        return chains

    # ── 메인 분석 진입점 ─────────────────────────────────────────────────
    def analyze(self, since_minutes: int = 15) -> List[CausalityChain]:
        """
        이벤트 수집 → 체인 구성 → 결과 반환.
        since_minutes: 현재 시점 기준 몇 분 이내 이벤트만 분석할지 (기본 15분)
        """
        events = self._fetch_warning_events(since_minutes=since_minutes)

        if not events:
            print(f"[INFO] 최근 {since_minutes}분 이내 Warning 이벤트 없음 - 이벤트 분석 스킵")
            return []

        chains = self._build_chains(events)

        # 발생 횟수(count)가 많은 Chain이 더 심각한 이벤트이므로 우선 정렬
        chains.sort(key=lambda c: c.root_cause.count, reverse=True)

        print(f"[INFO] 이벤트 체인 {len(chains)}건 (최근 {since_minutes}분 필터)")
        return chains
