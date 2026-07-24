"""
infra/metrics.py
====================
Prometheus 메트릭 수집 클라이언트
- 노드 CPU/메모리/디스크 사용률
- Pod 컨테이너 메모리 추이
- 재시작 횟수 급증 감지
- 네트워크 에러율
"""
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import requests


PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://192.168.67.13:30090")


@dataclass
class MetricPoint:
    timestamp: float
    value: float


@dataclass
class MetricResult:
    metric_name: str
    labels: Dict[str, str]
    points: List[MetricPoint] = field(default_factory=list)


class MetricsClient:
    """Prometheus HTTP API 클라이언트"""

    def __init__(self, url: str = None):
        self.url = (url or PROMETHEUS_URL).rstrip("/")

    def query_instant(self, promql: str) -> List[MetricResult]:
        """현재 시점 단일 쿼리"""
        try:
            resp = requests.get(
                f"{self.url}/api/v1/query",
                params={"query": promql},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Prometheus 쿼리 실패 ({promql[:50]}...): {e}")
            return []
        data = resp.json()
        if data["status"] != "success":
            print(f"[WARN] Prometheus 응답 오류: {data}")
            return []
        return self._parse_vector(data["data"]["result"])

    def query_range(
        self,
        promql: str,
        minutes: int = 10,
        step: str = "30s",
        reference_time: Optional[float] = None,
    ) -> List[MetricResult]:
        """과거 N분간 시계열 쿼리. reference_time이 주어지면 해당 시점을 end로 사용"""
        if reference_time is not None:
            end = datetime.utcfromtimestamp(reference_time)
        else:
            end = datetime.utcnow()
        start = end - timedelta(minutes=minutes)
        try:
            resp = requests.get(
                f"{self.url}/api/v1/query_range",
                params={
                    "query": promql,
                    "start": start.isoformat() + "Z",
                    "end": end.isoformat() + "Z",
                    "step": step,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Prometheus range 쿼리 실패 ({promql[:50]}...): {e}")
            return []
        data = resp.json()
        if data["status"] != "success":
            print(f"[WARN] Prometheus range 응답 오류: {data}")
            return []
        return self._parse_matrix(data["data"]["result"])

    def _parse_vector(self, results: list) -> List[MetricResult]:
        parsed = []
        for item in results:
            parsed.append(MetricResult(
                metric_name=item["metric"].get("__name__", ""),
                labels=item["metric"],
                points=[MetricPoint(
                    timestamp=float(item["value"][0]),
                    value=float(item["value"][1]),
                )],
            ))
        return parsed

    def _parse_matrix(self, results: list) -> List[MetricResult]:
        parsed = []
        for item in results:
            points = [
                MetricPoint(timestamp=float(v[0]), value=float(v[1]))
                for v in item["values"]
            ]
            parsed.append(MetricResult(
                metric_name=item["metric"].get("__name__", ""),
                labels=item["metric"],
                points=points,
            ))
        return parsed


class MetricsAnalyzer:
    """수집된 메트릭을 기반으로 이상 징후를 탐지하고 RCA 컨텍스트를 생성"""

    def __init__(self, client: MetricsClient = None):
        self.mc = client or MetricsClient()

    # ─── 노드 레벨 ───

    def get_node_cpu_usage(
        self, minutes: int = 10, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """노드별 CPU 사용률 (%)"""
        promql = (
            '100 - (avg by (instance) '
            '(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)'
        )
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    def get_node_memory_usage(
        self, minutes: int = 10, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """노드별 메모리 사용률 (%)"""
        promql = (
            '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
        )
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    def get_node_disk_usage(self) -> List[MetricResult]:
        """노드별 디스크 사용률 (%) - 현재 시점"""
        promql = (
            '100 - (node_filesystem_avail_bytes{mountpoint="/"} '
            '/ node_filesystem_size_bytes{mountpoint="/"} * 100)'
        )
        return self.mc.query_instant(promql)

    # ─── Pod 레벨 ───

    def get_pod_memory_usage(
        self, namespace: str = "", minutes: int = 10, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """Pod별 메모리 사용량 (bytes)"""
        ns_filter = f'namespace="{namespace}",' if namespace else ""
        promql = f'container_memory_working_set_bytes{{{ns_filter}container!="POD",container!=""}}'
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    def get_pod_cpu_usage(
        self, namespace: str = "", minutes: int = 10, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """Pod별 CPU 사용률"""
        ns_filter = f'namespace="{namespace}",' if namespace else ""
        promql = f'rate(container_cpu_usage_seconds_total{{{ns_filter}container!="POD",container!=""}}[2m])'
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    def get_pod_restart_rate(self, namespace: str = "", minutes: int = 30) -> List[MetricResult]:
        """Pod 재시작 횟수 증가율 (최근 N분) — 현재 시점 단일 쿼리"""
        ns_filter = f'namespace="{namespace}",' if namespace else ""
        promql = f'increase(kube_pod_container_status_restarts_total{{{ns_filter}}}[{minutes}m])'
        return self.mc.query_instant(promql)

    # ─── 네트워크 ───

    def get_network_errors(
        self, minutes: int = 10, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """네트워크 수신/송신 에러율"""
        promql = 'rate(node_network_receive_errs_total[2m]) + rate(node_network_transmit_errs_total[2m])'
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    def get_cpu_throttling(self, namespace: str = "") -> List[MetricResult]:
        """CPU throttling 비율 감지 (50% 이상이면 이상)"""
        ns_filter = f'namespace="{namespace}",' if namespace else ""
        promql = (
            f'rate(container_cpu_cfs_throttled_seconds_total{{{ns_filter}container!=""}}[2m])'
            f' / rate(container_cpu_cfs_periods_total{{{ns_filter}container!=""}}[2m])'
        )
        return self.mc.query_instant(promql)

    def get_network_drops(
        self, minutes: int = 5, reference_time: Optional[float] = None
    ) -> List[MetricResult]:
        """네트워크 패킷 드롭률 감지"""
        promql = 'rate(node_network_receive_drop_total[2m]) + rate(node_network_transmit_drop_total[2m])'
        return self.mc.query_range(promql, minutes=minutes, reference_time=reference_time)

    # ─── 이상 탐지 ───

    def detect_anomalies(
        self, namespace: str = "", reference_time: Optional[float] = None
    ) -> Dict[str, list]:
        """주요 이상 징후를 한번에 수집. reference_time으로 시점을 고정할 수 있음."""
        anomalies = {
            "high_cpu_nodes": [],
            "high_memory_nodes": [],
            "high_disk_nodes": [],
            "memory_spike_pods": [],
            "restart_spike_pods": [],
            "network_errors": [],
            "cpu_throttled_pods": [],
            "network_drops": [],
        }

        # 노드 CPU > 80%
        try:
            for r in self.get_node_cpu_usage(minutes=5, reference_time=reference_time):
                if r.points and r.points[-1].value > 80:
                    anomalies["high_cpu_nodes"].append({
                        "node": r.labels.get("instance", "unknown"),
                        "current_pct": round(r.points[-1].value, 1),
                        "trend": [round(p.value, 1) for p in r.points[-5:]],
                    })
        except Exception as e:
            print(f"[WARN] 노드 CPU 메트릭 수집 실패: {e}")

        # 노드 메모리 > 85%
        try:
            for r in self.get_node_memory_usage(minutes=5, reference_time=reference_time):
                if r.points and r.points[-1].value > 85:
                    anomalies["high_memory_nodes"].append({
                        "node": r.labels.get("instance", "unknown"),
                        "current_pct": round(r.points[-1].value, 1),
                        "trend": [round(p.value, 1) for p in r.points[-5:]],
                    })
        except Exception as e:
            print(f"[WARN] 노드 메모리 메트릭 수집 실패: {e}")

        # 노드 디스크 > 90%
        try:
            for r in self.get_node_disk_usage():
                if r.points and r.points[-1].value > 90:
                    anomalies["high_disk_nodes"].append({
                        "node": r.labels.get("instance", "unknown"),
                        "current_pct": round(r.points[-1].value, 1),
                    })
        except Exception as e:
            print(f"[WARN] 노드 디스크 메트릭 수집 실패: {e}")

        # Pod 재시작 급증 (최근 30분 내 3회 이상)
        try:
            for r in self.get_pod_restart_rate(namespace=namespace, minutes=30):
                if r.points and r.points[0].value >= 3:
                    anomalies["restart_spike_pods"].append({
                        "pod": r.labels.get("pod", "unknown"),
                        "namespace": r.labels.get("namespace", ""),
                        "restarts_in_30m": round(r.points[0].value),
                    })
        except Exception as e:
            print(f"[WARN] Pod 재시작 메트릭 수집 실패: {e}")

        # 네트워크 에러
        try:
            for r in self.get_network_errors(minutes=5, reference_time=reference_time):
                if r.points and r.points[-1].value > 0:
                    anomalies["network_errors"].append({
                        "device": r.labels.get("device", "unknown"),
                        "node": r.labels.get("instance", "unknown"),
                        "error_rate": round(r.points[-1].value, 4),
                    })
        except Exception as e:
            print(f"[WARN] 네트워크 에러 메트릭 수집 실패: {e}")

        # CPU throttling 감지 (throttle 비율 50% 이상)
        try:
            for r in self.get_cpu_throttling(namespace):
                if r.points and r.points[0].value > 0.5:
                    anomalies["cpu_throttled_pods"].append({
                        "pod": r.labels.get("pod", "unknown"),
                        "namespace": r.labels.get("namespace", ""),
                        "throttle_ratio": round(r.points[0].value, 2),
                    })
        except Exception as e:
            print(f"[WARN] CPU throttling 메트릭 수집 실패: {e}")

        # 네트워크 패킷 드롭 감지
        try:
            for r in self.get_network_drops(minutes=5, reference_time=reference_time):
                if r.points and r.points[-1].value > 0:
                    anomalies["network_drops"].append({
                        "node": r.labels.get("instance", "unknown"),
                        "device": r.labels.get("device", "unknown"),
                        "drop_rate": round(r.points[-1].value, 4),
                    })
        except Exception as e:
            print(f"[WARN] 네트워크 드롭 메트릭 수집 실패: {e}")

        return anomalies

    def build_metrics_context_for_llm(
        self, namespace: str = "", reference_time: Optional[float] = None
    ) -> str:
        """LLM에 전달할 메트릭 컨텍스트 문자열 생성. reference_time으로 시점 고정 가능."""
        anomalies = self.detect_anomalies(namespace, reference_time=reference_time)
        lines = ["\n--- [Prometheus 메트릭 이상 징후] ---\n"]

        if anomalies["high_cpu_nodes"]:
            lines.append("■ CPU 과부하 노드:")
            for n in anomalies["high_cpu_nodes"]:
                lines.append(f"  - {n['node']}: {n['current_pct']}% (추이: {n['trend']})")

        if anomalies["high_memory_nodes"]:
            lines.append("■ 메모리 과부하 노드:")
            for n in anomalies["high_memory_nodes"]:
                lines.append(f"  - {n['node']}: {n['current_pct']}% (추이: {n['trend']})")

        if anomalies["high_disk_nodes"]:
            lines.append("■ 디스크 부족 노드:")
            for n in anomalies["high_disk_nodes"]:
                lines.append(f"  - {n['node']}: {n['current_pct']}%")

        if anomalies["restart_spike_pods"]:
            lines.append("■ 재시작 급증 Pod:")
            for p in anomalies["restart_spike_pods"]:
                lines.append(f"  - {p['namespace']}/{p['pod']}: 최근 30분 내 {p['restarts_in_30m']}회 재시작")

        if anomalies["network_errors"]:
            lines.append("■ 네트워크 에러:")
            for e in anomalies["network_errors"]:
                lines.append(f"  - {e['node']} ({e['device']}): 에러율 {e['error_rate']}/s")

        if anomalies["cpu_throttled_pods"]:
            lines.append("■ CPU Throttling 과부하 Pod:")
            for p in anomalies["cpu_throttled_pods"]:
                lines.append(f"  - {p['namespace']}/{p['pod']}: throttle 비율 {p['throttle_ratio']*100:.0f}%")

        if anomalies["network_drops"]:
            lines.append("■ 네트워크 패킷 드롭:")
            for d in anomalies["network_drops"]:
                lines.append(f"  - {d['node']} ({d['device']}): 드롭률 {d['drop_rate']}/s")

        if len(lines) == 1:
            lines.append("  이상 징후 없음 (모든 메트릭 정상 범위)")

        return "\n".join(lines)
