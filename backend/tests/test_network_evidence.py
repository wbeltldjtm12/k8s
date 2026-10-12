"""Contracts for time-consistent Prometheus network-probe evidence."""

from __future__ import annotations

import unittest

from infra.metrics import (
    MetricPoint,
    MetricResult,
    MetricsQueryStatus,
)
from infra.network_evidence import (
    NETWORK_PROBE_FIELD_LABEL,
    NetworkEvidenceCollectionError,
    NetworkEvidenceCollector,
)


REFERENCE_TIME = 100.0


def batch_result(
    field: str,
    labels: dict[str, str],
    value: float,
) -> MetricResult:
    return MetricResult(
        metric_name="",
        labels={**labels, NETWORK_PROBE_FIELD_LABEL: field},
        points=[MetricPoint(timestamp=REFERENCE_TIME, value=value)],
    )


def path_batch(
    labels: dict[str, str],
    *,
    success: float = 1,
    samples: float = 4,
    latency: float = 0.01,
    failures: float = 0,
    observed_at: float = 98,
    scraped_at: float = 99,
    up: float = 1,
    up_scraped_at: float = 99,
) -> list[MetricResult]:
    values = {
        "success_min": success,
        "success_samples": samples,
        "latency_max": latency,
        "failure_increase": failures,
        "observed_at": observed_at,
        "scraped_at": scraped_at,
        "up": up,
        "up_scraped_at": up_scraped_at,
    }
    return [
        batch_result(field, labels, value)
        for field, value in values.items()
    ]


class FakeMetricsClient:
    def __init__(
        self,
        results: list[MetricResult],
        *,
        fail_query: bool = False,
    ) -> None:
        self.results = results
        self.fail_query = fail_query
        self.calls: list[tuple[str, float | None]] = []
        self.attempts = 0
        self.successes = 0
        self.failures = 0

    def get_query_status(self) -> MetricsQueryStatus:
        return MetricsQueryStatus(
            attempts=self.attempts,
            successes=self.successes,
            failures=self.failures,
        )

    def query_instant(
        self,
        promql: str,
        reference_time: float | None = None,
    ) -> list[MetricResult]:
        self.calls.append((promql, reference_time))
        self.attempts += 1
        if self.fail_query:
            self.failures += 1
            return []
        self.successes += 1
        return self.results


class NetworkEvidenceTests(unittest.TestCase):
    dns_labels = {
        "namespace": "sock-shop",
        "source_workload": "shipping",
        "source_pod": "shipping-123",
        "target": "catalogue",
        "probe": "dns",
    }
    http_labels = {
        "namespace": "sock-shop",
        "source_workload": "payment",
        "source_pod": "payment-123",
        "target": "payment",
        "probe": "http",
    }

    def collect(
        self,
        results: list[MetricResult],
        *,
        expected_sources: dict[str, str] | None = None,
        fail_query: bool = False,
    ):
        client = FakeMetricsClient(results, fail_query=fail_query)
        collector = NetworkEvidenceCollector(
            client=client,
            latency_warning_seconds=0.5,
            window_seconds=45,
            max_sample_age_seconds=10,
            max_probe_age_seconds=8,
            minimum_samples=2,
        )
        evidence = collector.collect(
            namespace="sock-shop",
            reference_time=REFERENCE_TIME,
            expected_sources=expected_sources,
        )
        return evidence, client

    def test_failed_dns_probe_becomes_error_evidence(self) -> None:
        evidence, client = self.collect(
            path_batch(self.dns_labels, success=0, failures=2),
            expected_sources={"shipping-123": "shipping"},
        )

        self.assertEqual(1, len(evidence))
        self.assertEqual("ERROR", evidence[0].status)
        self.assertEqual("DNSProbeFailed", evidence[0].reason)
        self.assertEqual("shipping-123", evidence[0].source_pod)
        self.assertEqual(45, evidence[0].window_seconds)
        self.assertEqual(1, len(client.calls))
        query, reference_time = client.calls[0]
        self.assertEqual(REFERENCE_TIME, reference_time)
        self.assertIn("increase(kubein_network_probe_failures_total", query)
        self.assertIn("timestamp(up", query)
        self.assertIn(" or ", query)

    def test_recent_failure_counter_survives_current_probe_recovery(self) -> None:
        evidence, _ = self.collect(
            path_batch(self.http_labels, success=1, failures=6),
            expected_sources={"payment-123": "payment"},
        )

        self.assertEqual(1, len(evidence))
        self.assertEqual("HTTPProbeFailed", evidence[0].reason)

    def test_high_latency_becomes_warning_when_reachable(self) -> None:
        evidence, _ = self.collect(
            path_batch(self.http_labels, latency=0.8),
            expected_sources={"payment-123": "payment"},
        )

        self.assertEqual(1, len(evidence))
        self.assertEqual("WARNING", evidence[0].status)
        self.assertEqual("HighNetworkLatency", evidence[0].reason)

    def test_successful_empty_batch_is_allowed_without_expected_sources(self) -> None:
        evidence, _ = self.collect([])
        self.assertEqual([], evidence)

    def test_missing_expected_probe_pod_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "missing current probe Pods",
        ):
            self.collect([], expected_sources={"shipping-123": "shipping"})

    def test_scrape_down_is_unavailable_not_root_cause(self) -> None:
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "scrape unavailable",
        ):
            self.collect(
                path_batch(self.http_labels, success=0, failures=4, up=0),
                expected_sources={"payment-123": "payment"},
            )

    def test_stale_probe_loop_observation_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "observed_at is stale",
        ):
            self.collect(
                path_batch(self.http_labels, observed_at=80),
                expected_sources={"payment-123": "payment"},
            )

    def test_stale_or_future_metric_timestamps_fail_closed(self) -> None:
        cases = (
            ("scraped_at", {"scraped_at": 80}),
            ("up_scraped_at", {"up_scraped_at": 80}),
            ("observed_at", {"observed_at": REFERENCE_TIME + 2}),
        )
        for field, overrides in cases:
            with self.subTest(field=field), self.assertRaisesRegex(
                NetworkEvidenceCollectionError,
                f"{field} is stale",
            ):
                self.collect(
                    path_batch(self.http_labels, **overrides),
                    expected_sources={"payment-123": "payment"},
                )

    def test_partial_metric_family_fails_closed(self) -> None:
        partial = [
            result
            for result in path_batch(self.http_labels)
            if result.labels[NETWORK_PROBE_FIELD_LABEL] != "latency_max"
        ]
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "incomplete",
        ):
            self.collect(
                partial,
                expected_sources={"payment-123": "payment"},
            )

    def test_duplicate_exact_path_field_fails_closed(self) -> None:
        duplicate = path_batch(self.http_labels)
        duplicate.append(
            batch_result("success_min", self.http_labels, 1)
        )
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "duplicate",
        ):
            self.collect(
                duplicate,
                expected_sources={"payment-123": "payment"},
            )

    def test_old_failed_pod_is_not_mixed_with_current_healthy_pod(self) -> None:
        old_labels = {**self.http_labels, "source_pod": "payment-old"}
        current_labels = {**self.http_labels, "source_pod": "payment-new"}
        evidence, _ = self.collect(
            path_batch(old_labels, success=0, failures=2)
            + path_batch(current_labels),
            expected_sources={"payment-new": "payment"},
        )
        self.assertEqual([], evidence)

    def test_two_current_replicas_keep_distinct_evidence(self) -> None:
        first_labels = {**self.http_labels, "source_pod": "payment-a"}
        second_labels = {**self.http_labels, "source_pod": "payment-b"}

        evidence, _ = self.collect(
            path_batch(first_labels, success=0, failures=2)
            + path_batch(second_labels, success=0, failures=3),
            expected_sources={
                "payment-a": "payment",
                "payment-b": "payment",
            },
        )

        self.assertEqual(2, len(evidence))
        self.assertEqual(
            {"payment-a", "payment-b"},
            {item.source_pod for item in evidence},
        )
        self.assertTrue(
            all(item.reason == "HTTPProbeFailed" for item in evidence)
        )

    def test_query_failure_is_distinct_from_successful_empty(self) -> None:
        with self.assertRaisesRegex(
            NetworkEvidenceCollectionError,
            "batch query failed",
        ):
            self.collect(
                [],
                expected_sources={"shipping-123": "shipping"},
                fail_query=True,
            )


if __name__ == "__main__":
    unittest.main()
