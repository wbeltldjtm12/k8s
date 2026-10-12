"""Contracts for bounded Prometheus requests used by Hybrid evaluation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

import requests

from infra.metrics import (
    DEFAULT_PROMETHEUS_REQUEST_TIMEOUT_SECONDS,
    METRICS_CONTEXT_QUERY_COUNT,
    MetricsAnalyzer,
    MetricsClient,
)


class MetricsTimeoutTests(unittest.TestCase):
    @staticmethod
    def successful_empty_response() -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success",
            "data": {"result": []},
        }
        return response

    def test_context_query_count_is_an_explicit_timeout_contract(self) -> None:
        self.assertEqual(7, METRICS_CONTEXT_QUERY_COUNT)

    def test_request_timeout_is_configurable_and_bounded(self) -> None:
        response = self.successful_empty_response()

        with patch.dict(
            os.environ,
            {"PROMETHEUS_REQUEST_TIMEOUT_SECONDS": "2.5"},
        ):
            client = MetricsClient("http://prometheus.invalid")
        with patch("infra.metrics.requests.get", return_value=response) as get:
            self.assertEqual([], client.query_instant("up"))

        self.assertEqual(2.5, get.call_args.kwargs["timeout"])

    def test_instant_query_can_be_pinned_to_snapshot_time(self) -> None:
        response = self.successful_empty_response()
        client = MetricsClient("http://prometheus.invalid")

        with patch("infra.metrics.requests.get", return_value=response) as get:
            self.assertEqual(
                [],
                client.query_instant("up", reference_time=1_700_000_000.25),
            )

        self.assertEqual(
            {
                "query": "up",
                "time": "1700000000.25",
            },
            get.call_args.kwargs["params"],
        )

    def test_query_range_uses_range_endpoint_and_configured_timeout(self) -> None:
        response = self.successful_empty_response()
        with patch.dict(
            os.environ,
            {"PROMETHEUS_REQUEST_TIMEOUT_SECONDS": "1.75"},
        ):
            client = MetricsClient("http://prometheus.invalid/")

        with patch("infra.metrics.requests.get", return_value=response) as get:
            self.assertEqual(
                [],
                client.query_range("up", minutes=3, reference_time=1_700_000_000),
            )

        self.assertEqual(
            "http://prometheus.invalid/api/v1/query_range",
            get.call_args.args[0],
        )
        self.assertEqual(1.75, get.call_args.kwargs["timeout"])
        self.assertEqual("up", get.call_args.kwargs["params"]["query"])

    def test_non_positive_non_finite_and_invalid_timeout_fall_back(self) -> None:
        for configured in ("invalid", "nan", "inf", "-inf", "0", "-1"):
            with self.subTest(configured=configured), patch.dict(
                os.environ,
                {"PROMETHEUS_REQUEST_TIMEOUT_SECONDS": configured},
            ):
                client = MetricsClient("http://prometheus.invalid")

                self.assertEqual(
                    DEFAULT_PROMETHEUS_REQUEST_TIMEOUT_SECONDS,
                    client.request_timeout,
                )

    def test_all_seven_timeouts_report_collection_failure(self) -> None:
        client = MetricsClient("http://prometheus.invalid")
        analyzer = MetricsAnalyzer(client)
        raw_error = "timeout at http://prometheus.invalid/raw-error"

        with patch(
            "infra.metrics.requests.get",
            side_effect=requests.exceptions.Timeout(raw_error),
        ) as get:
            context = analyzer.build_metrics_context_for_llm(namespace="sock-shop")

        status = client.get_query_status()
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, get.call_count)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.attempts)
        self.assertEqual(0, status.successes)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.failures)
        self.assertEqual(
            {"request_error": METRICS_CONTEXT_QUERY_COUNT},
            status.failure_categories,
        )
        self.assertIn("수집 실패 — 정상 여부 판단 불가", context)
        self.assertNotIn("모든 메트릭 정상 범위", context)
        self.assertNotIn(raw_error, context)
        self.assertNotIn("prometheus.invalid", context)

    def test_valid_empty_results_are_successful_and_normal(self) -> None:
        client = MetricsClient("http://prometheus.invalid")
        analyzer = MetricsAnalyzer(client)

        with patch(
            "infra.metrics.requests.get",
            return_value=self.successful_empty_response(),
        ) as get:
            context = analyzer.build_metrics_context_for_llm(namespace="sock-shop")

        status = client.get_query_status()
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, get.call_count)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.attempts)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.successes)
        self.assertEqual(0, status.failures)
        self.assertIn("이상 징후 없음 (모든 메트릭 정상 범위)", context)
        self.assertNotIn("수집 실패", context)

    def test_context_uses_query_status_delta_not_previous_failures(self) -> None:
        client = MetricsClient("http://prometheus.invalid")
        analyzer = MetricsAnalyzer(client)

        with patch(
            "infra.metrics.requests.get",
            side_effect=requests.exceptions.Timeout("previous failure"),
        ):
            self.assertEqual([], client.query_instant("up"))

        with patch(
            "infra.metrics.requests.get",
            return_value=self.successful_empty_response(),
        ):
            context = analyzer.build_metrics_context_for_llm(namespace="sock-shop")

        status = client.get_query_status()
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT + 1, status.attempts)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.successes)
        self.assertEqual(1, status.failures)
        self.assertIn("이상 징후 없음 (모든 메트릭 정상 범위)", context)
        self.assertNotIn("수집 실패", context)

    def test_partial_failure_warns_and_uses_successful_data(self) -> None:
        client = MetricsClient("http://prometheus.invalid")
        analyzer = MetricsAnalyzer(client)
        side_effects = [
            requests.exceptions.Timeout("raw timeout detail"),
            *[
                self.successful_empty_response()
                for _ in range(METRICS_CONTEXT_QUERY_COUNT - 1)
            ],
        ]

        with patch("infra.metrics.requests.get", side_effect=side_effects):
            context = analyzer.build_metrics_context_for_llm(namespace="sock-shop")

        status = client.get_query_status()
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT, status.attempts)
        self.assertEqual(METRICS_CONTEXT_QUERY_COUNT - 1, status.successes)
        self.assertEqual(1, status.failures)
        self.assertIn(
            "일부 메트릭 수집 실패 — 성공한 데이터 기준으로 판단",
            context,
        )
        self.assertIn("성공적으로 수집된 메트릭에서는 이상 징후 없음", context)
        self.assertNotIn("raw timeout detail", context)

    def test_json_schema_status_and_parse_failures_are_categorized(self) -> None:
        invalid_json = Mock()
        invalid_json.raise_for_status.return_value = None
        invalid_json.json.side_effect = ValueError("raw decoder detail")

        cases = (
            ("invalid_json", invalid_json),
            ("invalid_schema", self._response_with_payload([])),
            (
                "unsuccessful_status",
                self._response_with_payload({"status": "error", "error": "raw"}),
            ),
            (
                "parse_error",
                self._response_with_payload(
                    {
                        "status": "success",
                        "data": {"result": [{"metric": {}, "value": ["bad"]}]},
                    }
                ),
            ),
        )

        for expected_category, response in cases:
            with self.subTest(expected_category=expected_category):
                client = MetricsClient("http://prometheus.invalid")
                with patch("infra.metrics.requests.get", return_value=response):
                    self.assertEqual([], client.query_instant("up"))

                status = client.get_query_status()
                self.assertEqual(1, status.attempts)
                self.assertEqual(0, status.successes)
                self.assertEqual(1, status.failures)
                self.assertEqual(
                    {expected_category: 1},
                    status.failure_categories,
                )

    @staticmethod
    def _response_with_payload(payload: object) -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response


if __name__ == "__main__":
    unittest.main()
