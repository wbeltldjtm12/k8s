"""Contract tests for bounded LLM report-format repair."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import call, patch


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

from intelligence import llm as llm_module


VALID_ANALYSIS = """## 장애 요약
orders-db의 잘못된 볼륨 설정으로 Pod가 준비되지 않습니다.

## 근본 원인 (Root Cause)
- **근본 원인**: Deployment/orders-db
- **심각도 점수**: 10
- **상태**: ERROR
- **에러 이유**: NotReady
- **인과 체인**: Deployment/orders-db → Pod/orders-db
- **영향 범위**: 2개 리소스

## 인과 관계 분석
볼륨 마운트 실패 때문에 Pod가 Ready 상태에 도달하지 못합니다.

## 참고 문서
- https://kubernetes.io/docs/concepts/storage/volumes/

## 해결 방법
`kubectl describe deployment orders-db -n sock-shop`으로 상태를 확인합니다.
"""

MISSING_REFERENCE_HEADING = VALID_ANALYSIS.replace("## 참고 문서\n", "")


def response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


class SequenceCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item


class SequenceClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=SequenceCompletions(responses))


class ProviderHTTPError(RuntimeError):
    def __init__(self, retry_after: str):
        super().__init__("Error code: 429 - rate limit reached")
        self.status_code = 429
        self.response = SimpleNamespace(
            headers={"retry-after": retry_after},
            status_code=429,
        )


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(responses) -> llm_module.LLMClient:
    client = object.__new__(llm_module.LLMClient)
    client.model = "llama-3.1-8b-instant"
    client.max_output_tokens = 1600
    client.rag_max_docs = 1
    client.rag_max_chars_per_doc = 950
    client.quota_max_retries = 3
    client.quota_max_wait_seconds = 600
    client.quota_wait_grace_seconds = 0
    client.analysis_max_wall_seconds = 900
    client.format_repair_max_attempts = 1
    client.client = SequenceClient(responses)
    return client


class HeadingNormalizationTests(unittest.TestCase):
    def test_normalizes_only_explicit_atx_heading_aliases(self) -> None:
        aliased = (
            VALID_ANALYSIS
            .replace("## 장애 요약", "### 1. 장애 요약 :")
            .replace("## 근본 원인 (Root Cause)", "# 2) 근본 원인")
            .replace("## 인과 관계 분석", "###### 인과 관계 분석")
            .replace("## 참고 문서", "### 4. 참고 문서:")
            .replace("## 해결 방법", "#### 해결 방법 :")
        )

        normalized = llm_module.normalize_analysis_headings(aliased)

        self.assertEqual(VALID_ANALYSIS.strip(), normalized)

    def test_does_not_promote_prose_fuzzy_alias_or_fenced_text(self) -> None:
        unsafe_aliases = """장애 요약
본문의 일반 문장입니다.

## 장애 개요
fuzzy 동의어입니다.

```markdown
# 근본 원인
```
"""

        self.assertEqual(
            unsafe_aliases.strip(),
            llm_module.normalize_analysis_headings(unsafe_aliases),
        )

    def test_bom_crlf_bold_alias_is_idempotent(self) -> None:
        aliased = VALID_ANALYSIS.replace(
            "## 장애 요약\n",
            "\ufeff**1. 장애 요약:**\r\n",
        )

        once = llm_module.normalize_analysis_headings(aliased)
        twice = llm_module.normalize_analysis_headings(once)

        self.assertTrue(once.startswith("## 장애 요약\r\n"))
        self.assertEqual(once, twice)

    def test_normalizes_explicit_parenthetical_heading_translations(self) -> None:
        aliased = (
            VALID_ANALYSIS
            .replace(
                "## 장애 요약",
                "### 장애 요약 (Incident Summary)",
            )
            .replace(
                "## 근본 원인 (Root Cause)",
                "### 근본 원인 분석 (Root Cause Analysis)",
            )
            .replace(
                "## 인과 관계 분석",
                "### 인과 관계 분석 (Causal Relationship Analysis)",
            )
            .replace(
                "## 참고 문서",
                "### 참고 문서 (Official References)",
            )
            .replace(
                "## 해결 방법",
                "### 해결 방법 (Remediation Steps)",
            )
        )

        self.assertEqual(
            VALID_ANALYSIS.strip(),
            llm_module.normalize_analysis_headings(aliased),
        )

    def test_does_not_normalize_unknown_parenthetical_heading_suffix(self) -> None:
        unsafe_alias = VALID_ANALYSIS.replace(
            "## 장애 요약",
            "## 장애 요약 (Executive Interpretation)",
        )

        normalized = llm_module.normalize_analysis_headings(unsafe_alias)

        self.assertIn(
            "## 장애 요약 (Executive Interpretation)",
            normalized,
        )
        with self.assertRaisesRegex(ValueError, "heading|section|structure"):
            llm_module.validate_analysis_output(unsafe_alias, "stop")


class StrictStructureValidationTests(unittest.TestCase):
    def test_accepts_exactly_one_nonempty_section_in_required_order(self) -> None:
        self.assertEqual(
            VALID_ANALYSIS.strip(),
            llm_module.validate_analysis_output(VALID_ANALYSIS, "stop"),
        )

    def test_rejects_duplicate_heading(self) -> None:
        duplicate = VALID_ANALYSIS + "\n## 장애 요약\n중복 섹션입니다.\n"

        with self.assertRaisesRegex(ValueError, "heading|section|structure"):
            llm_module.validate_analysis_output(duplicate, "stop")

    def test_rejects_heading_order_change(self) -> None:
        wrong_order = (
            VALID_ANALYSIS
            .replace("## 인과 관계 분석", "## __TEMP__")
            .replace("## 참고 문서", "## 인과 관계 분석")
            .replace("## __TEMP__", "## 참고 문서")
        )

        with self.assertRaisesRegex(ValueError, "heading|order|structure"):
            llm_module.validate_analysis_output(wrong_order, "stop")

    def test_rejects_unexpected_atx_sections(self) -> None:
        for extra_heading in (
            "## 대체 근본 원인",
            "### Other Root Cause",
        ):
            with self.subTest(extra_heading=extra_heading):
                invalid = (
                    VALID_ANALYSIS
                    + f"\n{extra_heading}\n"
                    + "Deployment/catalogue가 추가 원인입니다.\n"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "unexpected|heading|structure",
                ):
                    llm_module.validate_analysis_output(invalid, "stop")

    def test_rejects_unexpected_setext_sections(self) -> None:
        for underline in ("---", "==="):
            with self.subTest(underline=underline):
                invalid = (
                    VALID_ANALYSIS
                    + "\nOther Root Cause\n"
                    + underline
                    + "\nDeployment/catalogue가 추가 원인입니다.\n"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "unexpected|heading|structure",
                ):
                    llm_module.validate_analysis_output(invalid, "stop")

    def test_rejects_empty_section_body(self) -> None:
        empty_reference = VALID_ANALYSIS.replace(
            "## 참고 문서\n"
            "- https://kubernetes.io/docs/concepts/storage/volumes/\n\n"
            "## 해결 방법",
            "## 참고 문서\n\n"
            "## 해결 방법\n"
            "참고: https://kubernetes.io/docs/concepts/storage/volumes/",
        )

        with self.assertRaisesRegex(ValueError, "empty|section|structure"):
            llm_module.validate_analysis_output(empty_reference, "stop")

    def test_fenced_heading_does_not_satisfy_missing_section(self) -> None:
        fenced_reference = MISSING_REFERENCE_HEADING.replace(
            "## 해결 방법",
            "```markdown\n## 참고 문서\n```\n\n## 해결 방법",
        )

        with self.assertRaisesRegex(ValueError, "heading|section|structure"):
            llm_module.validate_analysis_output(fenced_reference, "stop")

    def test_four_space_indented_code_is_not_promoted_to_heading(self) -> None:
        indented_reference = VALID_ANALYSIS.replace(
            "## 참고 문서",
            "    ## 참고 문서",
        )

        with self.assertRaisesRegex(ValueError, "heading|section|structure"):
            llm_module.validate_analysis_output(indented_reference, "stop")

    def test_fence_with_trailing_info_does_not_close_active_block(self) -> None:
        fake_close = VALID_ANALYSIS.replace(
            "## 참고 문서",
            "```markdown\n```not-a-closing-fence\n## 참고 문서",
        )

        with self.assertRaisesRegex(ValueError, "fence|heading|structure"):
            llm_module.validate_analysis_output(fake_close, "stop")

    def test_rejects_redirect_url_containing_official_url_in_query(self) -> None:
        invalid = VALID_ANALYSIS.replace(
            "https://kubernetes.io/docs/concepts/storage/volumes/",
            "https://evil.invalid/redirect?"
            "next=https://kubernetes.io/docs/concepts/storage/volumes/",
        )

        with self.assertRaisesRegex(ValueError, "official|URL"):
            llm_module.validate_analysis_output(invalid, "stop")

    def test_rejects_nonofficial_url_alongside_official_reference(self) -> None:
        invalid = VALID_ANALYSIS.replace(
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
            "- https://kubernetes.io/docs/concepts/storage/volumes/\n"
            "- https://evil.invalid/untrusted",
        )

        with self.assertRaisesRegex(ValueError, "official|URL"):
            llm_module.validate_analysis_output(invalid, "stop")

    def test_rejects_nonofficial_url_outside_reference_section(self) -> None:
        invalid = VALID_ANALYSIS.replace(
            "orders-db의 잘못된 볼륨 설정으로 Pod가 준비되지 않습니다.",
            "외부 진단 링크 https://evil.invalid/untrusted 를 확인합니다.",
        )

        with self.assertRaisesRegex(ValueError, "official|URL"):
            llm_module.validate_analysis_output(invalid, "stop")

    def test_requires_official_url_in_reference_section(self) -> None:
        invalid = VALID_ANALYSIS.replace(
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
            "- 공식 문서를 확인합니다.",
        ) + (
            "\n해결 참고: "
            "https://kubernetes.io/docs/concepts/storage/volumes/\n"
        )

        with self.assertRaisesRegex(ValueError, "official|URL"):
            llm_module.validate_analysis_output(invalid, "stop")

    def test_rejects_scale_commands_with_flags_plural_and_aliases(self) -> None:
        commands = (
            "kubectl scale --namespace sock-shop pod catalogue --replicas=1",
            "kubectl scale pods catalogue --replicas=1",
            "kubectl --namespace sock-shop scale service front-end --replicas=1",
            "kubectl scale -n sock-shop svc front-end --replicas=1",
            "/usr/bin/kubectl scale po catalogue --replicas=1",
            "kubectl scale -o yaml service front-end --replicas=1",
            "kubectl scale --output yaml pod catalogue --replicas=1",
            "kubectl scale --dry-run client pods catalogue --replicas=1",
        )

        for command in commands:
            with self.subTest(command=command):
                invalid = VALID_ANALYSIS + f"\n`{command}`\n"
                with self.assertRaisesRegex(
                    ValueError,
                    "unsupported kubectl scale",
                ):
                    llm_module.validate_analysis_output(invalid, "stop")

    def test_allows_scale_deployment_command(self) -> None:
        valid = (
            VALID_ANALYSIS
            + "\n`kubectl -n sock-shop scale deployment catalogue "
            "--replicas=1`\n"
        )

        self.assertEqual(
            valid.strip(),
            llm_module.validate_analysis_output(valid, "stop"),
        )

    def test_unknown_finish_reason_is_not_exposed(self) -> None:
        raw_sentinel = "RAW_FINISH_REASON_PRIVATE_0a71"

        with self.assertRaises(Exception) as caught:
            llm_module.validate_analysis_output(
                VALID_ANALYSIS,
                raw_sentinel,
            )

        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertIn("finish_reason=unknown", str(caught.exception))


class HeadingRepairDiagnosticsTests(unittest.TestCase):
    def test_reports_eligible_missing_heading_without_content(self) -> None:
        diagnostics = llm_module._heading_repair_diagnostics(
            MISSING_REFERENCE_HEADING,
            "stop",
        )

        self.assertTrue(diagnostics.candidate)
        self.assertEqual("eligible", diagnostics.reason)
        self.assertEqual(4, diagnostics.present_heading_count)
        self.assertEqual(1, diagnostics.missing_heading_count)
        self.assertTrue(diagnostics.balanced_code_fence)
        self.assertTrue(diagnostics.has_official_url)
        self.assertFalse(diagnostics.has_unsupported_scale_command)
        self.assertFalse(diagnostics.has_duplicate_headings)
        self.assertTrue(diagnostics.has_valid_heading_order)

    def test_reports_too_few_headings_without_exposing_draft(self) -> None:
        raw_sentinel = "RAW_DRAFT_PRIVATE_DIAGNOSTIC_91b7"
        invalid = (
            VALID_ANALYSIS
            .replace("orders-db의 잘못된 볼륨 설정", raw_sentinel)
            .replace("## 인과 관계 분석\n", "")
            .replace("## 참고 문서\n", "")
            .replace("## 해결 방법\n", "")
        )
        diagnostics = llm_module._heading_repair_diagnostics(
            invalid,
            "stop",
        )

        self.assertFalse(diagnostics.candidate)
        self.assertEqual(
            "too_few_recognized_headings",
            diagnostics.reason,
        )
        self.assertEqual(2, diagnostics.present_heading_count)
        self.assertEqual(3, diagnostics.missing_heading_count)
        self.assertTrue(diagnostics.balanced_code_fence)
        self.assertTrue(diagnostics.has_official_url)
        self.assertFalse(diagnostics.has_unsupported_scale_command)

        client = make_client([response(invalid)])
        with patch("builtins.print") as log:
            with self.assertRaises(Exception) as caught:
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertIn("phase=primary", rendered_log)
        self.assertIn("repair_candidate=false", rendered_log)
        self.assertIn(
            "reason=too_few_recognized_headings",
            rendered_log,
        )
        self.assertIn("present_headings=2", rendered_log)
        self.assertIn("missing_headings=3", rendered_log)
        self.assertIn("balanced_fence=true", rendered_log)
        self.assertIn("official_url=true", rendered_log)
        self.assertIn("unsupported_scale=false", rendered_log)
        self.assertIn("action=skip_not_candidate", rendered_log)
        self.assertNotIn(raw_sentinel, rendered_log)
        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_only_missing_reference_heading_is_repairable(self) -> None:
        missing_causal_heading = VALID_ANALYSIS.replace(
            "## 인과 관계 분석\n",
            "",
        )
        diagnostics = llm_module._heading_repair_diagnostics(
            missing_causal_heading,
            "stop",
        )

        self.assertFalse(diagnostics.candidate)
        self.assertEqual("unsupported_missing_heading", diagnostics.reason)
        self.assertEqual(4, diagnostics.present_heading_count)
        self.assertEqual(1, diagnostics.missing_heading_count)

        client = make_client([response(missing_causal_heading)])
        with self.assertRaisesRegex(Exception, "무결성"):
            client.analyze("Deployment/orders-db is NotReady")
        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_logs_rejected_repair_structure_without_raw_draft(self) -> None:
        raw_sentinel = "RAW_REPAIR_PRIVATE_DIAGNOSTIC_e2c4"
        invalid_repair = MISSING_REFERENCE_HEADING.replace(
            "orders-db의 잘못된 볼륨 설정",
            raw_sentinel,
        )
        client = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                response(invalid_repair),
            ]
        )

        with patch("builtins.print") as log:
            with self.assertRaises(Exception) as caught:
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertIn("phase=primary", rendered_log)
        self.assertIn("action=repair", rendered_log)
        self.assertIn("phase=repair", rendered_log)
        self.assertIn("action=reject", rendered_log)
        self.assertNotIn(raw_sentinel, rendered_log)
        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_long_quota_wait_still_reports_candidate_skip_reason(self) -> None:
        invalid = (
            VALID_ANALYSIS
            .replace("## 인과 관계 분석\n", "")
            .replace("## 참고 문서\n", "")
            .replace("## 해결 방법\n", "")
        )
        clock = FakeClock()
        client = make_client(
            [
                ProviderHTTPError("503"),
                response(invalid),
            ]
        )

        with (
            patch("intelligence.llm.time.monotonic", clock.monotonic),
            patch(
                "intelligence.llm.time.sleep",
                side_effect=clock.advance,
            ),
            patch("builtins.print") as log,
        ):
            with self.assertRaises(Exception):
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertIn("quota_wait_seconds=503", rendered_log)
        self.assertIn("quota_retries_used=1", rendered_log)
        self.assertIn("provider_attempts=2", rendered_log)
        self.assertIn("wall_remaining_seconds=397", rendered_log)
        self.assertIn(
            "reason=too_few_recognized_headings",
            rendered_log,
        )
        self.assertIn("action=skip_not_candidate", rendered_log)
        self.assertEqual(2, len(client.client.chat.completions.calls))


class ConditionalFormattingRepairTests(unittest.TestCase):
    def test_heading_structure_failure_gets_one_deterministic_repair(self) -> None:
        client = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                response(VALID_ANALYSIS),
            ]
        )

        result = client.analyze(
            "Deployment/orders-db is NotReady",
            ["Official URL: https://kubernetes.io/docs/concepts/storage/volumes/"],
        )

        self.assertEqual(VALID_ANALYSIS.strip(), result)
        calls = client.client.chat.completions.calls
        self.assertEqual(2, len(calls))
        self.assertEqual(0.2, calls[0]["temperature"])
        self.assertEqual(0, calls[1]["temperature"])
        self.assertEqual(client.max_output_tokens, calls[1]["max_tokens"])
        repair_prompt = calls[1]["messages"][-1]["content"]
        self.assertIn("--- [수정할 초안] ---", repair_prompt)
        self.assertIn(MISSING_REFERENCE_HEADING.strip(), repair_prompt)

    def test_heading_normalization_does_not_spend_repair_call(self) -> None:
        aliased = VALID_ANALYSIS.replace(
            "## 근본 원인 (Root Cause)",
            "### 근본 원인",
        )
        client = make_client([response(aliased)])

        result = client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(VALID_ANALYSIS.strip(), result)
        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_non_format_failures_never_trigger_repair(self) -> None:
        cases = {
            "truncated": response(VALID_ANALYSIS, "length"),
            "empty": response(""),
            "missing_url": response(
                VALID_ANALYSIS.replace(
                    "- https://kubernetes.io/docs/concepts/storage/volumes/\n",
                    "공식 문서를 확인합니다.\n",
                )
            ),
            "unbalanced_fence": response(VALID_ANALYSIS + "\n```bash\n"),
            "unsupported_command": response(
                VALID_ANALYSIS + "\nkubectl scale pod orders-db --replicas=1\n"
            ),
            "none_content": response(None),
        }

        for name, provider_response in cases.items():
            with self.subTest(name=name):
                client = make_client([provider_response])
                with self.assertRaises(Exception):
                    client.analyze("Deployment/orders-db is NotReady")
                self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_repair_is_disabled_when_attempt_limit_is_zero(self) -> None:
        client = make_client([response(MISSING_REFERENCE_HEADING)])
        client.format_repair_max_attempts = 0

        with self.assertRaisesRegex(Exception, "무결성"):
            client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_attempt_limit_is_clamped_to_one(self) -> None:
        client = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                response(MISSING_REFERENCE_HEADING),
            ]
        )
        client.format_repair_max_attempts = 99

        with self.assertRaisesRegex(Exception, "무결성"):
            client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_semantic_drift_in_repaired_output_is_rejected(self) -> None:
        drifted = VALID_ANALYSIS.replace(
            "Deployment/orders-db",
            "Deployment/catalogue",
        )
        client = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                response(drifted),
            ]
        )

        with self.assertRaisesRegex(Exception, "무결성|drift"):
            client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_failed_draft_is_not_exposed_in_logs_or_exception(self) -> None:
        raw_sentinel = "RAW_DRAFT_PRIVATE_8e1275"
        invalid = MISSING_REFERENCE_HEADING.replace(
            "orders-db의 잘못된 볼륨 설정",
            raw_sentinel,
        )
        client = make_client([response(invalid), response(invalid)])

        with patch("builtins.print") as log:
            with self.assertRaises(Exception) as caught:
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertNotIn(raw_sentinel, rendered_log)
        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_provider_echo_during_repair_does_not_expose_draft(self) -> None:
        raw_sentinel = "RAW_PROVIDER_ECHO_PRIVATE_72f0"
        client = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                RuntimeError(
                    "provider rejected request body: "
                    + raw_sentinel
                ),
            ]
        )

        with patch("builtins.print") as log:
            with self.assertRaises(Exception) as caught:
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertNotIn(raw_sentinel, rendered_log)
        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertIn(
            "provider_request_failed",
            str(caught.exception),
        )
        self.assertEqual(2, len(client.client.chat.completions.calls))

    def test_provider_finish_reason_is_allowlisted_before_logging(self) -> None:
        raw_sentinel = "RAW_FINISH_REASON_LOG_PRIVATE_f4d9"
        provider_response = response(
            VALID_ANALYSIS,
            raw_sentinel,
        )
        provider_response.usage = SimpleNamespace(
            prompt_tokens=raw_sentinel,
            completion_tokens=1,
            total_tokens=2,
        )
        client = make_client([provider_response])

        with patch("builtins.print") as log:
            with self.assertRaises(Exception) as caught:
                client.analyze("Deployment/orders-db is NotReady")

        rendered_log = " ".join(
            str(part)
            for logged_call in log.call_args_list
            for part in logged_call.args
        )
        self.assertNotIn(raw_sentinel, rendered_log)
        self.assertNotIn(raw_sentinel, str(caught.exception))
        self.assertIn("prompt=N/A", rendered_log)
        self.assertIn("finish=unknown", rendered_log)
        self.assertIn("finish_reason=unknown", str(caught.exception))
        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_semantic_guard_preserves_exact_whitespace(self) -> None:
        valid_repair = VALID_ANALYSIS
        self.assertTrue(
            llm_module._repair_preserves_semantics(
                MISSING_REFERENCE_HEADING,
                valid_repair,
            )
        )

        trailing_space = valid_repair.replace(
            "`kubectl describe deployment orders-db -n sock-shop`",
            "`kubectl describe deployment orders-db -n sock-shop` ",
        )
        extra_blank_line = valid_repair.replace(
            "볼륨 마운트 실패 때문에 Pod가 Ready 상태에 도달하지 못합니다.\n\n",
            "볼륨 마운트 실패 때문에 Pod가 Ready 상태에 도달하지 못합니다.\n\n\n",
        )

        self.assertFalse(
            llm_module._repair_preserves_semantics(
                MISSING_REFERENCE_HEADING,
                trailing_space,
            )
        )
        self.assertFalse(
            llm_module._repair_preserves_semantics(
                MISSING_REFERENCE_HEADING,
                extra_blank_line,
            )
        )

    def test_semantic_guard_preserves_existing_heading_anchors(self) -> None:
        moved_root_heading = VALID_ANALYSIS.replace(
            "## 근본 원인 (Root Cause)\n"
            "- **근본 원인**: Deployment/orders-db\n",
            "- **근본 원인**: Deployment/orders-db\n"
            "## 근본 원인 (Root Cause)\n",
        )
        llm_module.validate_analysis_output(
            moved_root_heading,
            "stop",
        )

        self.assertFalse(
            llm_module._repair_preserves_semantics(
                MISSING_REFERENCE_HEADING,
                moved_root_heading,
            )
        )

    def test_semantic_guard_requires_reference_heading_at_url_anchor(self) -> None:
        draft = MISSING_REFERENCE_HEADING.replace(
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
            "참고 링크 설명입니다.\n"
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
        )
        safe_repair = draft.replace(
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
            "## 참고 문서\n"
            "- https://kubernetes.io/docs/concepts/storage/volumes/",
        )
        arbitrary_boundary = draft.replace(
            "참고 링크 설명입니다.",
            "## 참고 문서\n참고 링크 설명입니다.",
        )
        llm_module.validate_analysis_output(safe_repair, "stop")
        llm_module.validate_analysis_output(arbitrary_boundary, "stop")

        self.assertTrue(
            llm_module._repair_preserves_semantics(draft, safe_repair)
        )
        self.assertFalse(
            llm_module._repair_preserves_semantics(
                draft,
                arbitrary_boundary,
            )
        )


class SharedAnalysisBudgetTests(unittest.TestCase):
    def test_primary_and_repair_share_quota_sleep_budget(self) -> None:
        client = make_client(
            [
                ProviderHTTPError("4"),
                response(MISSING_REFERENCE_HEADING),
                ProviderHTTPError("2"),
            ]
        )
        client.quota_max_wait_seconds = 5

        with patch("intelligence.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(Exception, "quota_exhausted"):
                client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual([call(4)], sleep.call_args_list)
        self.assertEqual(3, len(client.client.chat.completions.calls))

    def test_primary_and_repair_share_quota_retry_count(self) -> None:
        client = make_client(
            [
                ProviderHTTPError("1"),
                response(MISSING_REFERENCE_HEADING),
                ProviderHTTPError("1"),
            ]
        )
        client.quota_max_retries = 1

        with patch("intelligence.llm.time.sleep") as sleep:
            with self.assertRaisesRegex(Exception, "quota_exhausted"):
                client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual([call(1)], sleep.call_args_list)
        self.assertEqual(3, len(client.client.chat.completions.calls))

    def test_elapsed_primary_call_prevents_repair_past_wall_deadline(self) -> None:
        clock = FakeClock()

        def slow_invalid_response():
            clock.advance(6)
            return response(MISSING_REFERENCE_HEADING)

        client = make_client([slow_invalid_response])
        client.analysis_max_wall_seconds = 5

        with patch("intelligence.llm.time.monotonic", clock.monotonic):
            with self.assertRaisesRegex(Exception, "wall|deadline|budget|timeout"):
                client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(1, len(client.client.chat.completions.calls))

    def test_three_quota_retries_are_shared_across_primary_and_repair(self) -> None:
        client = make_client(
            [
                ProviderHTTPError("1"),
                ProviderHTTPError("1"),
                response(MISSING_REFERENCE_HEADING),
                ProviderHTTPError("1"),
                response(VALID_ANALYSIS),
            ]
        )

        with patch("intelligence.llm.time.sleep") as sleep:
            result = client.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(VALID_ANALYSIS.strip(), result)
        self.assertEqual([call(1), call(1), call(1)], sleep.call_args_list)
        self.assertEqual(5, len(client.client.chat.completions.calls))

    def test_global_slot_is_held_through_repair_and_released_after_success(self) -> None:
        contender = make_client([response(VALID_ANALYSIS)])

        def assert_slot_held():
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                contender.check_connection()
            return response(MISSING_REFERENCE_HEADING)

        holder = make_client([assert_slot_held, response(VALID_ANALYSIS)])

        self.assertEqual(
            VALID_ANALYSIS.strip(),
            holder.analyze("Deployment/orders-db is NotReady"),
        )
        self.assertEqual(
            {"model": "llama-3.1-8b-instant"},
            contender.check_connection(),
        )

    def test_global_slot_is_released_after_failed_repair(self) -> None:
        holder = make_client(
            [
                response(MISSING_REFERENCE_HEADING),
                response(MISSING_REFERENCE_HEADING),
            ]
        )
        contender = make_client([response(VALID_ANALYSIS)])

        with self.assertRaises(Exception):
            holder.analyze("Deployment/orders-db is NotReady")

        self.assertEqual(
            {"model": "llama-3.1-8b-instant"},
            contender.check_connection(),
        )


if __name__ == "__main__":
    unittest.main()
