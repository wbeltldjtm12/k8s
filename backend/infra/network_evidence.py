"""Convert time-bounded network-probe samples into graph-ready evidence."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from infra.metrics import MetricResult, MetricsClient


NETWORK_PROBE_SUCCESS_METRIC = "kubein_network_probe_success"
NETWORK_PROBE_LATENCY_METRIC = "kubein_network_probe_latency_seconds"
NETWORK_PROBE_FAILURE_METRIC = "kubein_network_probe_failures_total"
NETWORK_PROBE_OBSERVED_METRIC = (
    "kubein_network_probe_observed_timestamp_seconds"
)
NETWORK_PROBE_FIELD_LABEL = "kubein_field"
NETWORK_PROBE_CONTAINER = "kubein-netprobe"
REQUIRED_BATCH_FIELDS = {
    "success_min",
    "success_samples",
    "latency_max",
    "failure_increase",
    "observed_at",
    "scraped_at",
    "up",
    "up_scraped_at",
}


class NetworkEvidenceCollectionError(RuntimeError):
    """Raised when enabled probe evidence cannot form a trustworthy snapshot."""


@dataclass(frozen=True)
class NetworkEvidence:
    """A deterministic observation of one workload network path."""

    namespace: str
    source_workload: str
    source_pod: str
    target: str
    probe: str
    status: str
    reason: str
    message: str
    latency_seconds: Optional[float] = None
    observed_at: Optional[float] = None
    window_seconds: int = 0


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _path_key(labels: Mapping[str, str]) -> tuple[str, str, str, str, str]:
    return (
        labels.get("namespace", ""),
        labels.get("source_workload", ""),
        labels.get("source_pod", labels.get("pod", "")),
        labels.get("target", ""),
        labels.get("probe", ""),
    )


def _single_finite_value(result: MetricResult) -> float:
    if len(result.points) != 1:
        raise NetworkEvidenceCollectionError(
            "network evidence batch field must contain exactly one point"
        )
    value = result.points[0].value
    if not math.isfinite(value):
        raise NetworkEvidenceCollectionError(
            "network evidence batch contains a non-finite value"
        )
    return value


def _label_field(expression: str, field: str) -> str:
    return (
        f'label_replace(({expression}), "{NETWORK_PROBE_FIELD_LABEL}", '
        f'"{field}", "probe", ".*")'
    )


def _build_batch_query(namespace: str, window_seconds: int) -> str:
    escaped_namespace = _escape_label_value(namespace)
    probe_selector = f'{{namespace="{escaped_namespace}"}}'
    up_selector = (
        f'{{namespace="{escaped_namespace}",container="{NETWORK_PROBE_CONTAINER}",'
        'source_workload!="",source_pod!="",target!="",probe!=""}'
    )
    window = f"[{window_seconds}s]"
    expressions = (
        _label_field(
            f"min_over_time({NETWORK_PROBE_SUCCESS_METRIC}{probe_selector}{window})",
            "success_min",
        ),
        _label_field(
            f"count_over_time({NETWORK_PROBE_SUCCESS_METRIC}{probe_selector}{window})",
            "success_samples",
        ),
        _label_field(
            f"max_over_time({NETWORK_PROBE_LATENCY_METRIC}{probe_selector}{window})",
            "latency_max",
        ),
        _label_field(
            f"increase({NETWORK_PROBE_FAILURE_METRIC}{probe_selector}{window})",
            "failure_increase",
        ),
        _label_field(
            f"max_over_time({NETWORK_PROBE_OBSERVED_METRIC}{probe_selector}{window})",
            "observed_at",
        ),
        _label_field(
            f"timestamp({NETWORK_PROBE_SUCCESS_METRIC}{probe_selector})",
            "scraped_at",
        ),
        _label_field(f"up{up_selector}", "up"),
        _label_field(f"timestamp(up{up_selector})", "up_scraped_at"),
    )
    return " or ".join(f"({expression})" for expression in expressions)


class NetworkEvidenceCollector:
    """Collect an all-or-nothing batch for the current probe Pod set."""

    def __init__(
        self,
        client: MetricsClient | None = None,
        latency_warning_seconds: float | None = None,
        window_seconds: int | None = None,
        max_sample_age_seconds: float | None = None,
        max_probe_age_seconds: float | None = None,
        minimum_samples: int | None = None,
    ) -> None:
        self.client = client or MetricsClient()
        self.latency_warning_seconds = (
            latency_warning_seconds
            if latency_warning_seconds is not None
            else _positive_float_env(
                "KUBEIN_NETWORK_PROBE_LATENCY_SECONDS",
                0.5,
            )
        )
        self.window_seconds = (
            window_seconds
            if window_seconds is not None
            else _positive_int_env("KUBEIN_NETWORK_PROBE_WINDOW_SECONDS", 45)
        )
        self.max_sample_age_seconds = (
            max_sample_age_seconds
            if max_sample_age_seconds is not None
            else _positive_float_env(
                "KUBEIN_NETWORK_PROBE_MAX_SAMPLE_AGE_SECONDS",
                10.0,
            )
        )
        self.max_probe_age_seconds = (
            max_probe_age_seconds
            if max_probe_age_seconds is not None
            else _positive_float_env(
                "KUBEIN_NETWORK_PROBE_MAX_OBSERVATION_AGE_SECONDS",
                8.0,
            )
        )
        self.minimum_samples = (
            minimum_samples
            if minimum_samples is not None
            else _positive_int_env("KUBEIN_NETWORK_PROBE_MIN_SAMPLES", 2)
        )

    def _query_batch(
        self,
        namespace: str,
        reference_time: float,
    ) -> list[MetricResult]:
        status_reader = getattr(self.client, "get_query_status", None)
        status_before = status_reader() if callable(status_reader) else None
        results = self.client.query_instant(
            _build_batch_query(namespace, self.window_seconds),
            reference_time=reference_time,
        )
        status_after = status_reader() if callable(status_reader) else None
        if status_before is not None and status_after is not None:
            attempts = status_after.attempts - status_before.attempts
            successes = status_after.successes - status_before.successes
            failures = status_after.failures - status_before.failures
            if attempts != 1 or successes != 1 or failures != 0:
                raise NetworkEvidenceCollectionError(
                    "Prometheus network evidence batch query failed"
                )
        return results

    def collect(
        self,
        namespace: str = "",
        *,
        reference_time: float | None = None,
        expected_sources: Mapping[str, str] | None = None,
    ) -> list[NetworkEvidence]:
        reference_time = reference_time if reference_time is not None else time.time()
        expected_sources = dict(expected_sources or {})
        rows = self._query_batch(namespace, reference_time)

        grouped: dict[
            tuple[str, str, str, str, str],
            dict[str, float],
        ] = {}
        for result in rows:
            field = result.labels.get(NETWORK_PROBE_FIELD_LABEL, "")
            if field not in REQUIRED_BATCH_FIELDS:
                raise NetworkEvidenceCollectionError(
                    f"unexpected network evidence batch field: {field or '<empty>'}"
                )
            key = _path_key(result.labels)
            ns, source_workload, source_pod, target, probe = key
            if (
                not ns
                or not source_workload
                or not source_pod
                or not target
                or probe not in {"dns", "http"}
            ):
                raise NetworkEvidenceCollectionError(
                    "network evidence batch contains incomplete labels"
                )
            if namespace and ns != namespace:
                raise NetworkEvidenceCollectionError(
                    "network evidence batch crossed the requested namespace"
                )
            if expected_sources:
                expected_workload = expected_sources.get(source_pod)
                if expected_workload is None:
                    continue
                if expected_workload != source_workload:
                    raise NetworkEvidenceCollectionError(
                        "network evidence source Pod/workload labels disagree"
                    )
            fields = grouped.setdefault(key, {})
            if field in fields:
                raise NetworkEvidenceCollectionError(
                    "duplicate network evidence path field"
                )
            fields[field] = _single_finite_value(result)

        evidence: list[NetworkEvidence] = []
        observed_source_pods: set[str] = set()
        for key in sorted(grouped):
            ns, source_workload, source_pod, target, probe = key
            fields = grouped[key]
            missing = REQUIRED_BATCH_FIELDS - fields.keys()
            if missing:
                raise NetworkEvidenceCollectionError(
                    "network evidence batch is incomplete: "
                    + ",".join(sorted(missing))
                )
            if fields["up"] != 1:
                raise NetworkEvidenceCollectionError(
                    f"network probe scrape unavailable: {ns}/{source_pod}"
                )
            if fields["success_min"] not in {0, 1}:
                raise NetworkEvidenceCollectionError(
                    "network probe success value must be exactly 0 or 1"
                )
            if fields["success_samples"] < self.minimum_samples:
                raise NetworkEvidenceCollectionError(
                    f"network probe has fewer than {self.minimum_samples} samples"
                )
            for timestamp_field, maximum_age in (
                ("scraped_at", self.max_sample_age_seconds),
                ("up_scraped_at", self.max_sample_age_seconds),
                ("observed_at", self.max_probe_age_seconds),
            ):
                age = reference_time - fields[timestamp_field]
                if age < -1.0 or age > maximum_age:
                    raise NetworkEvidenceCollectionError(
                        f"network probe {timestamp_field} is stale"
                    )
            if fields["failure_increase"] < 0:
                raise NetworkEvidenceCollectionError(
                    "network probe failure counter increase cannot be negative"
                )

            observed_source_pods.add(source_pod)
            failure_observed = fields["failure_increase"] > 0
            success_failed = fields["success_min"] < 1
            if failure_observed or success_failed:
                reason = (
                    "DNSProbeFailed" if probe == "dns" else "HTTPProbeFailed"
                )
                message = (
                    f"{source_workload} workload {probe} probe to {target} "
                    f"failed in the last {self.window_seconds}s"
                )
                evidence.append(
                    NetworkEvidence(
                        namespace=ns,
                        source_workload=source_workload,
                        source_pod=source_pod,
                        target=target,
                        probe=probe,
                        status="ERROR",
                        reason=reason,
                        message=message,
                        latency_seconds=fields["latency_max"],
                        observed_at=fields["observed_at"],
                        window_seconds=self.window_seconds,
                    )
                )
            elif fields["latency_max"] >= self.latency_warning_seconds:
                evidence.append(
                    NetworkEvidence(
                        namespace=ns,
                        source_workload=source_workload,
                        source_pod=source_pod,
                        target=target,
                        probe=probe,
                        status="WARNING",
                        reason="HighNetworkLatency",
                        message=(
                            f"{source_workload} workload {probe} probe to {target} "
                            f"took {fields['latency_max']:.3f}s "
                            f"(threshold={self.latency_warning_seconds:.3f}s, "
                            f"window={self.window_seconds}s)"
                        ),
                        latency_seconds=fields["latency_max"],
                        observed_at=fields["observed_at"],
                        window_seconds=self.window_seconds,
                    )
                )

        if expected_sources:
            missing_sources = set(expected_sources) - observed_source_pods
            if missing_sources:
                raise NetworkEvidenceCollectionError(
                    "network evidence missing current probe Pods: "
                    + ",".join(sorted(missing_sources))
                )
        return evidence


def collect_network_evidence(
    namespace: str = "",
    client: MetricsClient | None = None,
    *,
    reference_time: float | None = None,
    expected_sources: Mapping[str, str] | None = None,
) -> list[NetworkEvidence]:
    """Convenience entry point used by the graph augmentation path."""
    return NetworkEvidenceCollector(client=client).collect(
        namespace=namespace,
        reference_time=reference_time,
        expected_sources=expected_sources,
    )
