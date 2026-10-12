"""Unit tests for the explicit LLM connectivity probe."""

from __future__ import annotations

import os
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


try:
    import chromadb  # noqa: F401
    from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: F401
except ModuleNotFoundError:
    chromadb_stub = ModuleType("chromadb")
    chromadb_stub.PersistentClient = object
    chromadb_utils_stub = ModuleType("chromadb.utils")
    chromadb_utils_stub.embedding_functions = type(
        "EmbeddingFunctions", (), {"DefaultEmbeddingFunction": object}
    )
    splitters_stub = ModuleType("langchain_text_splitters")
    splitters_stub.RecursiveCharacterTextSplitter = object
    sys.modules.setdefault("chromadb", chromadb_stub)
    sys.modules.setdefault("chromadb.utils", chromadb_utils_stub)
    sys.modules.setdefault("langchain_text_splitters", splitters_stub)

from intelligence.llm import (
    LLMClient,
    Retriever,
    _positive_float_env,
    build_rag_query,
    parse_retry_after_seconds,
    redact_provider_error,
    validate_analysis_output,
)

VALID_ANALYSIS = """## 장애 요약
테스트 장애입니다.

## 근본 원인 (Root Cause)
- **근본 원인**: Deployment/catalogue

## 인과 관계 분석
Pod 실패가 Deployment 가용성에 영향을 줍니다.

## 참고 문서
- https://kubernetes.io/docs/concepts/workloads/controllers/deployment/

## 해결 방법
Deployment 상태를 확인합니다.
"""


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = type("Chat", (), {"completions": FakeCompletions(response)})()


class SequenceCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class SequenceClient:
    def __init__(self, responses):
        self.chat = type(
            "Chat",
            (),
            {"completions": SequenceCompletions(responses)},
        )()


class ProviderHTTPError(RuntimeError):
    def __init__(self, message, retry_after=None, status_code=429):
        super().__init__(message)
        self.status_code = status_code
        headers = {} if retry_after is None else {"retry-after": retry_after}
        self.response = type(
            "ProviderResponse",
            (),
            {"headers": headers, "status_code": status_code},
        )()


class RaisingCompletions:
    def create(self, **kwargs):
        raise RuntimeError(
            "Error code: 429 Rate limit for organization `org_private` with key "
            "gsk_private_value"
        )


class FakeRaisingClient:
    chat = type("Chat", (), {"completions": RaisingCompletions()})()


class FakeResponse:
    choices = [object()]


class FakeAnalysisResponse:
    choices = [
        type(
            "Choice",
            (),
            {
                "message": type(
                    "Message",
                    (),
                    {"content": VALID_ANALYSIS},
                )(),
                "finish_reason": "stop",
            },
        )()
    ]


class LLMHealthTests(unittest.TestCase):
    def test_probe_uses_bounded_minimal_completion(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "gemini-2.5-flash"
        client.client = FakeClient(FakeResponse())

        self.assertEqual({"model": "gemini-2.5-flash"}, client.check_connection())

        call = client.client.chat.completions.calls[0]
        self.assertEqual("gemini-2.5-flash", call["model"])
        self.assertEqual(1, call["max_tokens"])
        self.assertEqual(0, call["temperature"])

    def test_probe_rejects_empty_response(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "gemini-2.5-flash"
        client.client = FakeClient(type("EmptyResponse", (), {"choices": []})())

        with self.assertRaisesRegex(RuntimeError, "no choices"):
            client.check_connection()

    def test_probe_does_not_expose_provider_controlled_error(self) -> None:
        raw_sentinel = "RAW_PROVIDER_HEALTH_PRIVATE_41f9"
        client = object.__new__(LLMClient)
        client.model = "gemini-2.5-flash"
        client.client = SequenceClient(
            [RuntimeError(raw_sentinel), FakeResponse()]
        )

        with self.assertRaises(Exception) as caught:
            client.check_connection()

        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertIn("request_failed", str(caught.exception))
        self.assertEqual(
            {"model": "gemini-2.5-flash"},
            client.check_connection(),
        )

    def test_provider_slot_is_shared_across_client_instances(self) -> None:
        holder = object.__new__(LLMClient)
        contender = object.__new__(LLMClient)
        contender.model = "llama-3.1-8b-instant"
        contender.client = FakeClient(FakeAnalysisResponse())
        request_slot = holder._take_request_slot()
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "already in progress",
            ):
                contender._create_analysis_completion(model=contender.model)
            with self.assertRaisesRegex(
                RuntimeError,
                "already in progress",
            ):
                contender.check_connection()
        finally:
            request_slot.release()

        self.assertEqual(0, len(contender.client.chat.completions.calls))

    def test_analysis_uses_configured_output_limit(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.client = FakeClient(FakeAnalysisResponse())

        self.assertEqual(
            VALID_ANALYSIS.strip(),
            client.analyze("Pod is not ready"),
        )

        call = client.client.chat.completions.calls[0]
        self.assertEqual(1000, call["max_tokens"])
        self.assertEqual(0.2, call["temperature"])
        user_prompt = call["messages"][1]["content"]
        self.assertIn("900 토큰 이내", user_prompt)
        self.assertIn("kubectl scale service", user_prompt)
        self.assertIn("kubectl scale pod", user_prompt)

    def test_analysis_bounds_rag_context(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.client = FakeClient(FakeAnalysisResponse())

        client.analyze(
            "Pod is not ready",
            ["A" * 1000, "B" * 1000],
        )

        user_prompt = client.client.chat.completions.calls[0]["messages"][1]["content"]
        self.assertIn("A" * 950, user_prompt)
        self.assertNotIn("A" * 951, user_prompt)
        self.assertNotIn("B" * 100, user_prompt)

    def test_analysis_redacts_provider_identifiers(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.client = FakeRaisingClient()

        with self.assertRaises(Exception) as caught:
            client.analyze("Pod is not ready")

        message = str(caught.exception)
        self.assertNotIn("org_private", message)
        self.assertNotIn("gsk_private_value", message)
        self.assertEqual("LLM quota_exhausted", message)

    def test_redactor_covers_health_and_url_credential_shapes(self) -> None:
        unsafe = (
            "organization org_private project=proj_private "
            '"request_id":"req_private" Authorization: Bearer bearer.private '
            "sk-private123456789 AIza12345678901234567890 "
            "https://user:password@example.invalid/v1?api_key=query_private"
        )

        safe = redact_provider_error(unsafe)

        for secret in (
            "org_private",
            "proj_private",
            "req_private",
            "bearer.private",
            "sk-private123456789",
            "AIza12345678901234567890",
            "user:password",
            "query_private",
        ):
            self.assertNotIn(secret, safe)

    def test_retriever_prompt_context_preserves_official_source(self) -> None:
        retriever = object.__new__(Retriever)
        retriever.search = lambda query: [
            {
                "source": "debug-running-pod.md",
                "url": "https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/",
                "text": "Inspect the Pod with kubectl describe.",
            }
        ]

        context = retriever.search_texts_only("CrashLoopBackOff")

        self.assertEqual(1, len(context))
        self.assertIn("Source: debug-running-pod.md", context[0])
        self.assertIn("Official URL: https://kubernetes.io/", context[0])
        self.assertIn("kubectl describe", context[0])

    def test_output_validator_rejects_truncation_and_invalid_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "finish_reason=length"):
            validate_analysis_output(VALID_ANALYSIS, "length")

        invalid = (
            VALID_ANALYSIS
            + "\n```bash\nkubectl scale pod catalogue --replicas=1\n```\n"
        )
        with self.assertRaisesRegex(ValueError, "unsupported kubectl scale"):
            validate_analysis_output(invalid, "stop")

    def test_rag_query_uses_detected_symptoms(self) -> None:
        summary = (
            "Kubernetes 클러스터 이상 노드 2개:\n"
            "- Pod/sock-shop/catalogue (원인: CrashLoopBackOff)\n"
            "- Deployment/sock-shop/catalogue (원인: NoAvailableReplicas)"
        )

        query = build_rag_query([], summary)

        self.assertIn("CrashLoopBackOff", query)
        self.assertIn("NoAvailableReplicas", query)
        self.assertIn("Pod", query)
        self.assertNotIn("healthy no errors", query)

    def test_quota_retry_waits_for_provider_window(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.quota_max_retries = 1
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [
                RuntimeError(
                    "Error code: 429 - Rate limit reached; "
                    "Please try again in 0.1s"
                ),
                FakeAnalysisResponse(),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            result = client.analyze("Pod is not ready")

        self.assertEqual(VALID_ANALYSIS.strip(), result)
        sleep.assert_called_once_with(1)
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_quota_retry_uses_header_without_body_hint(self) -> None:
        client = object.__new__(LLMClient)
        client.quota_max_retries = 1
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [
                ProviderHTTPError("quota temporarily unavailable", "1.2"),
                FakeAnalysisResponse(),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            response = client._create_analysis_completion(model="test")

        self.assertIsInstance(response, FakeAnalysisResponse)
        sleep.assert_called_once_with(2)
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_quota_retry_prefers_header_over_body_hint(self) -> None:
        client = object.__new__(LLMClient)
        client.quota_max_retries = 1
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [
                ProviderHTTPError(
                    "Error code: 429 - Rate limit; try again in 9s",
                    "2",
                ),
                FakeAnalysisResponse(),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            client._create_analysis_completion(model="test")

        sleep.assert_called_once_with(2)

    def test_invalid_retry_header_falls_back_without_logging_header(self) -> None:
        client = object.__new__(LLMClient)
        client.quota_max_retries = 1
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        header_secret = "gsk_header_secret"
        client.client = SequenceClient(
            [
                ProviderHTTPError(
                    "Error code: 429 - Rate limit; try again in 3s",
                    header_secret,
                ),
                FakeAnalysisResponse(),
            ]
        )

        with (
            patch("intelligence.llm.time.sleep") as sleep,
            patch("builtins.print") as log,
        ):
            client._create_analysis_completion(model="test")

        sleep.assert_called_once_with(3)
        rendered_log = " ".join(
            str(part)
            for call in log.call_args_list
            for part in call.args
        )
        self.assertNotIn(header_secret, rendered_log)

    def test_retry_header_over_remaining_budget_does_not_retry(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.quota_max_retries = 1
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [ProviderHTTPError("quota temporarily unavailable", "11")]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(Exception, "quota_exhausted"):
                client.analyze("Pod is not ready")

        sleep.assert_not_called()
        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_quota_retry_uses_multiple_windows_with_one_total_cap(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.quota_max_retries = 3
        client.quota_max_wait_seconds = 10
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [
                RuntimeError(
                    "Error code: 429 - Rate limit reached; "
                    "Please try again in 1.1s"
                ),
                RuntimeError(
                    "Error code: 429 - Rate limit reached; "
                    "Please try again in 2.1s"
                ),
                FakeAnalysisResponse(),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            result = client.analyze("Pod is not ready")

        self.assertEqual(VALID_ANALYSIS.strip(), result)
        self.assertEqual([unittest.mock.call(2), unittest.mock.call(3)], sleep.call_args_list)
        self.assertEqual(3, len(client.client.chat.completions.calls))

    def test_quota_retry_never_exceeds_total_wait_budget(self) -> None:
        client = object.__new__(LLMClient)
        client.model = "llama-3.1-8b-instant"
        client.max_output_tokens = 1000
        client.rag_max_docs = 1
        client.rag_max_chars_per_doc = 950
        client.quota_max_retries = 3
        client.quota_max_wait_seconds = 5
        client.quota_wait_grace_seconds = 0
        client.client = SequenceClient(
            [
                RuntimeError(
                    "Error code: 429 - Rate limit reached; "
                    "Please try again in 4s"
                ),
                RuntimeError(
                    "Error code: 429 - Rate limit reached; "
                    "Please try again in 2s"
                ),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(Exception, "quota_exhausted"):
                client.analyze("Pod is not ready")

        sleep.assert_called_once_with(4)
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_retry_after_parser_rounds_up(self) -> None:
        self.assertEqual(
            392,
            parse_retry_after_seconds("Please try again in 6m31.2192s"),
        )
        self.assertEqual(360, parse_retry_after_seconds("try again in 6m"))
        self.assertEqual(1, parse_retry_after_seconds("try again in 125ms"))
        self.assertEqual(0, parse_retry_after_seconds("no retry hint"))

    def test_positive_float_env_rejects_non_finite_and_non_positive(self) -> None:
        for raw_value in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(raw_value=raw_value):
                with patch.dict(
                    os.environ,
                    {"TEST_POSITIVE_FLOAT": raw_value},
                ):
                    self.assertEqual(
                        12.5,
                        _positive_float_env("TEST_POSITIVE_FLOAT", 12.5),
                    )


if __name__ == "__main__":
    unittest.main()
