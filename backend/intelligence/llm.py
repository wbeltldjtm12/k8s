"""
intelligence/llm.py
====================
LLM 분석 + RAG 문서 로딩/검색 통합 모듈
"""

import hashlib
import math
import os
import re
import shlex
import time
from dataclasses import dataclass
from threading import Lock
from typing import List, Dict
from urllib.parse import urlsplit

import chromadb
from chromadb.utils import embedding_functions
from langchain_text_splitters import RecursiveCharacterTextSplitter


# ── RAG 관련 설정 및 상수 ──────────────────────
URL_MAP = {
    "audit.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/audit/",
    "crictl.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/crictl/",
    "debug-deployment.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/debug-deployment/",
    "debug-pods.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/debug-pods/",
    "debug-running-pod.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/debug-running-pod/",
    "debug-service.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/debug-service/",
    "debug-statefulset.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/debug-statefulset/",
    "determine-reason-pod-failure.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/determine-reason-pod-failure/",
    "dns-debugging-resolution.md": "https://kubernetes.io/ko/docs/tasks/administer-cluster/dns-debugging-resolution/",
    "get-shell-running-container.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/get-shell-running-container/",
    "kubectl-node-debug.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/kubectl-node-debug/",
    "local-debugging.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-application/local-debugging/",
    "monitor-node-health.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/monitor-node-health/",
    "persistent-volumes.md": "https://kubernetes.io/ko/concepts/storage/persistent-volumes/",
    "resource-metrics-pipeline.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/",
    "resource-usage-monitoring.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/resource-usage-monitoring/",
    "topology.md": "https://kubernetes.io/ko/docs/tasks/administer-cluster/topology-manager/",
    "troubleshoot-kubectl.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/",
    "windows.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/windows/",
    "debug-cluster.md": "https://kubernetes.io/ko/docs/tasks/debug/debug-cluster/",
}

BASE_DIR = os.environ.get("DOCS_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "asd")))
CHROMA_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "chroma_db"))
COLLECTION_NAME = "k8s_docs"
REQUIRED_ANALYSIS_HEADINGS = (
    "## 장애 요약",
    "## 근본 원인 (Root Cause)",
    "## 인과 관계 분석",
    "## 참고 문서",
    "## 해결 방법",
)
_PROVIDER_REQUEST_LOCK = Lock()
_ANALYSIS_BUDGET_SAFETY_SECONDS = 5.0
_ALLOWED_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "function_call",
    }
)


class AnalysisIntegrityError(ValueError):
    """Raw provider output을 노출하지 않는 구조화된 응답 무결성 오류."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"LLM 응답 무결성 실패: {code}")


class AnalysisBudgetError(RuntimeError):
    """한 분석 요청에 허용된 monotonic wall budget을 소진한 경우."""


class ProviderRequestError(RuntimeError):
    """Provider-controlled 오류 본문을 폐기한 안정적인 내부 오류."""

    def __init__(self, code: str, status_code: int = None):
        self.code = code
        self.status_code = status_code
        rendered_status = (
            str(status_code)
            if isinstance(status_code, int)
            else "unknown"
        )
        super().__init__(
            "LLM provider request failed: "
            f"code={code} status={rendered_status}"
        )


@dataclass
class _AnalysisRequestBudget:
    deadline: float
    quota_max_retries: int
    quota_max_wait_seconds: int
    quota_retries_used: int = 0
    quota_wait_seconds: int = 0
    provider_attempts: int = 0


@dataclass(frozen=True)
class _HeadingRepairDiagnostics:
    candidate: bool
    reason: str
    present_heading_count: int
    missing_heading_count: int
    balanced_code_fence: bool
    has_official_url: bool
    has_invalid_url: bool
    has_unsupported_scale_command: bool
    has_duplicate_headings: bool
    has_valid_heading_order: bool
    unexpected_heading_count: int


def build_rag_query(root_causes, error_summary: str = "") -> str:
    """오류 종류와 reason만 남긴 짧은 troubleshooting 검색어를 만든다."""
    terms: List[str] = []

    def add_term(value) -> None:
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)

    for reason in re.findall(
        r"(?:원인|Reason):\s*([A-Za-z][A-Za-z0-9_-]+)",
        error_summary or "",
    ):
        add_term(reason)
    for kind in re.findall(
        r"(?:^|\n)-\s*([A-Za-z][A-Za-z0-9_-]*)/",
        error_summary or "",
    ):
        add_term(kind)
    for root_cause in (root_causes or [])[:3]:
        add_term(getattr(root_cause, "root_cause_reason", ""))
        add_term(getattr(root_cause, "root_cause_kind", ""))

    if not terms:
        return "Kubernetes cluster healthy no errors"
    return (
        "Kubernetes troubleshooting "
        + " ".join(terms[:10])
        + " kubectl logs describe events"
    )


# ── DocLoader (ChromaDB에 문서 임베딩) ──────────────────────
class DocLoader:
    """Kubernetes 공식 문서를 ChromaDB에 임베딩하는 로더"""

    def __init__(self):
        if not os.path.exists(BASE_DIR):
            print(f"[WARN] 문서 디렉토리 없음: {BASE_DIR}")
        self.client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        self.ef = embedding_functions.DefaultEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self.ef,
        )

    def load_local_md_files(self):
        """문서 폴더 내의 .md 파일들을 읽어서 ChromaDB에 저장합니다."""
        documents = []
        metadatas = []
        ids = []

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=800, chunk_overlap=100,
            separators=["\n\n", "\n", " ", ""],
        )

        print(f"[INFO] 문서 폴더: {BASE_DIR}")

        if not os.path.isdir(BASE_DIR):
            print(f"[WARN] 폴더가 존재하지 않습니다: {BASE_DIR}")
            return

        for filename in os.listdir(BASE_DIR):
            if not filename.endswith(".md"):
                continue

            file_path = os.path.join(BASE_DIR, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()

            chunks = splitter.split_text(text)

            for i, chunk in enumerate(chunks):
                documents.append(chunk)
                metadatas.append({
                    "source": filename,
                    "url": URL_MAP.get(filename, "")
                })
                ids.append(f"{filename}_{i}")

        if documents:
            self.collection.upsert(
                documents=documents,
                metadatas=metadatas,
                ids=ids,
            )
            print(f"[INFO] {len(documents)}개 청크 저장 완료 (DB: {CHROMA_DB_PATH})")
        else:
            print("[WARN] 임베딩할 .md 파일을 찾지 못했습니다.")


# ── Retriever (유사 문서 검색) ──────────────────────────────
class Retriever:
    """ChromaDB에서 유사 문서를 검색하는 RAG 리트리버"""

    def __init__(self, top_k: int = 1):
        self.top_k = top_k
        try:
            ef = embedding_functions.DefaultEmbeddingFunction()
            client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
            self.collection = client.get_or_create_collection(
                COLLECTION_NAME,
                embedding_function=ef,
            )
            self._ready = self.collection.count() > 0
            if not self._ready:
                print("[WARN] ChromaDB가 비어있습니다. DocLoader를 통해 문서를 먼저 임베딩하세요.")
            else:
                print(f"[INFO] ChromaDB 로드 완료 ({self.collection.count()}개 청크, 경로: {CHROMA_DB_PATH})")
        except Exception as e:
            print(f"[WARN] ChromaDB 초기화 실패: {e}")
            self._ready = False

    def search(self, query: str) -> List[Dict[str, str]]:
        """쿼리와 의미적으로 유사한 문서 청크를 반환합니다."""
        if not self._ready or not query.strip():
            return []

        try:
            n = min(self.top_k, self.collection.count())
            results = self.collection.query(
                query_texts=[query],
                n_results=n,
                include=["documents", "metadatas"],
            )

            docs = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]

            return [
                {
                    "text": doc,
                    "source": meta.get("source", ""),
                    "url": meta.get("url", ""),
                }
                for doc, meta in zip(docs, metadatas)
            ]

        except Exception as e:
            print(f"[WARN] RAG 검색 오류: {e}")
            return []

    def search_texts_only(self, query: str) -> List[str]:
        """LLM 프롬프트에 주입할 텍스트만 반환하는 편의 메서드."""
        rendered = []
        for doc in self.search(query):
            parts = []
            if doc.get("source"):
                parts.append(f"Source: {doc['source']}")
            if doc.get("url"):
                parts.append(f"Official URL: {doc['url']}")
            parts.append(doc.get("text", ""))
            rendered.append("\n".join(part for part in parts if part))
        return rendered


# ── LLMClient (OpenAI 호환 로컬 LLM) ───────────────────────
class LLMClient:
    """OpenAI 호환 API를 사용하는 LLM 클라이언트. vLLM, Ollama 등 로컬 서버에 연결합니다."""

    def __init__(
        self,
        base_url: str = None,
        model: str = None,
        api_key: str = None,
    ):
        from openai import OpenAI
        self.request_timeout = _positive_float_env("LLM_REQUEST_TIMEOUT_SECONDS", 45.0)
        # Quota 재시도는 아래의 명시적 bounded retry가 담당한다. SDK의
        # 암묵 재시도까지 겹치면 runner의 HTTP timeout 상한을 넘길 수 있다.
        self.max_retries = _non_negative_int_env("LLM_MAX_RETRIES", 0)
        self.max_output_tokens = _positive_int_env("LLM_MAX_OUTPUT_TOKENS", 1000)
        self.rag_max_docs = _positive_int_env("LLM_RAG_MAX_DOCS", 1)
        self.rag_max_chars_per_doc = _positive_int_env(
            "LLM_RAG_MAX_CHARS_PER_DOC", 950
        )
        self.quota_max_retries = _non_negative_int_env(
            "LLM_QUOTA_MAX_RETRIES", 3
        )
        self.quota_max_wait_seconds = _positive_int_env(
            "LLM_QUOTA_MAX_WAIT_SECONDS", 600
        )
        self.quota_wait_grace_seconds = _non_negative_int_env(
            "LLM_QUOTA_WAIT_GRACE_SECONDS", 3
        )
        self.analysis_max_wall_seconds = _positive_float_env(
            "LLM_ANALYSIS_MAX_WALL_SECONDS", 900.0
        )
        self.format_repair_max_attempts = min(
            1,
            _non_negative_int_env("LLM_FORMAT_REPAIR_MAX_ATTEMPTS", 1),
        )
        self.client = OpenAI(
            base_url=base_url or os.environ.get("LLM_BASE_URL", "http://222.105.251.70:30135/v1"),
            api_key=api_key or os.environ.get("LLM_API_KEY", "not-needed"),
            timeout=self.request_timeout,
            max_retries=self.max_retries,
        )
        self.model = model or os.environ.get("LLM_MODEL", "qwen3-32b-gpu3-test")

    def _new_analysis_budget(self) -> _AnalysisRequestBudget:
        wall_seconds = float(
            getattr(self, "analysis_max_wall_seconds", 900.0)
        )
        if not math.isfinite(wall_seconds) or wall_seconds <= 0:
            wall_seconds = 900.0
        return _AnalysisRequestBudget(
            deadline=time.monotonic() + wall_seconds,
            quota_max_retries=max(
                0,
                int(getattr(self, "quota_max_retries", 3)),
            ),
            quota_max_wait_seconds=max(
                1,
                int(getattr(self, "quota_max_wait_seconds", 600)),
            ),
        )

    @staticmethod
    def _ensure_budget_active(
        budget: _AnalysisRequestBudget,
        stage: str,
    ) -> None:
        if time.monotonic() >= budget.deadline:
            raise AnalysisBudgetError(
                f"LLM analysis wall budget exhausted before {stage}"
            )

    def _ensure_budget_for_additional_call(
        self,
        budget: _AnalysisRequestBudget,
        *,
        wait_seconds: float = 0,
        stage: str,
    ) -> None:
        self._ensure_budget_active(budget, stage)
        request_timeout = float(getattr(self, "request_timeout", 45.0))
        if not math.isfinite(request_timeout) or request_timeout < 0:
            request_timeout = 45.0
        required = (
            max(0.0, float(wait_seconds))
            + request_timeout
            + _ANALYSIS_BUDGET_SAFETY_SECONDS
        )
        if budget.deadline - time.monotonic() < required:
            raise AnalysisBudgetError(
                f"LLM analysis wall budget exhausted before {stage}"
            )

    def _create_analysis_completion(
        self,
        *,
        budget: _AnalysisRequestBudget = None,
        phase: str = "request",
        **kwargs,
    ):
        request_lock = self._take_request_slot()
        try:
            return self._create_analysis_completion_unlocked(
                budget=budget or self._new_analysis_budget(),
                phase=phase,
                **kwargs,
            )
        finally:
            if request_lock is not None:
                request_lock.release()

    def _take_request_slot(self):
        """동시 provider 호출은 기다리지 않고 실패시켜 quota herd를 방지한다."""
        request_lock = _PROVIDER_REQUEST_LOCK
        if not request_lock.acquire(blocking=False):
            raise RuntimeError("LLM request already in progress")
        return request_lock

    def _create_analysis_completion_unlocked(
        self,
        *,
        budget: _AnalysisRequestBudget = None,
        phase: str = "request",
        **kwargs,
    ):
        budget = budget or self._new_analysis_budget()
        grace = max(0, getattr(self, "quota_wait_grace_seconds", 3))
        first_attempt = True

        while True:
            if first_attempt:
                self._ensure_budget_active(budget, phase)
                first_attempt = False
            else:
                self._ensure_budget_for_additional_call(
                    budget,
                    stage=f"{phase} provider retry",
                )
            budget.provider_attempts += 1
            try:
                response = self.client.chat.completions.create(**kwargs)
                self._ensure_budget_active(budget, f"{phase} validation")
                return response
            except Exception as exc:
                if isinstance(exc, AnalysisBudgetError):
                    raise
                safe_error = redact_provider_error(str(exc))
                retry_after = _retry_after_seconds(exc, safe_error)
                is_quota_error = _is_quota_error(exc, safe_error)
                remaining_wait = (
                    budget.quota_max_wait_seconds
                    - budget.quota_wait_seconds
                )
                wait_seconds = retry_after + grace
                can_retry = (
                    is_quota_error
                    and retry_after > 0
                    and budget.quota_retries_used
                    < budget.quota_max_retries
                    and remaining_wait > 0
                    and wait_seconds <= remaining_wait
                )
                if not can_retry:
                    raise _sanitized_provider_error(
                        exc,
                        safe_error=safe_error,
                        is_quota_error=is_quota_error,
                    ) from None
                self._ensure_budget_for_additional_call(
                    budget,
                    wait_seconds=wait_seconds,
                    stage=f"{phase} quota retry",
                )
                budget.quota_retries_used += 1
                budget.quota_wait_seconds += wait_seconds
                print(
                    "[WARN] LLM quota_exhausted; "
                    f"{wait_seconds}s 후 재시도 "
                    f"({budget.quota_retries_used}/"
                    f"{budget.quota_max_retries}, "
                    f"누적 {budget.quota_wait_seconds}/"
                    f"{budget.quota_max_wait_seconds}s, "
                    f"phase={phase})"
                )
                time.sleep(wait_seconds)

    def check_connection(self) -> Dict[str, str]:
        """Make a bounded, explicit LLM request for operator diagnostics.

        This is intentionally separate from the regular service health endpoint:
        polling a generative API as a container liveness check would create
        needless latency and API cost.
        """
        request_lock = self._take_request_slot()
        try:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    temperature=0,
                    max_tokens=1,
                )
            except Exception as exc:
                safe_error = redact_provider_error(str(exc))
                raise _sanitized_provider_error(
                    exc,
                    safe_error=safe_error,
                    is_quota_error=_is_quota_error(exc, safe_error),
                ) from None
        finally:
            if request_lock is not None:
                request_lock.release()
        if not getattr(response, "choices", None):
            raise RuntimeError("LLM probe returned no choices")
        return {"model": self.model}

    @staticmethod
    def _repair_messages(draft: str):
        headings = "\n".join(REQUIRED_ANALYSIS_HEADINGS)
        return [
            {
                "role": "system",
                "content": (
                    "You are a deterministic Markdown formatter. "
                    "Only insert or replace section heading lines. "
                    "Do not change, add, remove, reorder, or translate any "
                    "non-heading content, URL, command, resource identifier, "
                    "number, or RCA claim."
                ),
            },
            {
                "role": "user",
                "content": (
                    "아래 초안의 본문은 한 글자도 바꾸지 말고, 섹션 제목만 "
                    "다음 다섯 줄과 순서에 정확히 맞추세요.\n\n"
                    f"{headings}\n\n"
                    "--- [수정할 초안] ---\n"
                    f"{draft}"
                ),
            },
        ]

    @staticmethod
    def _completion_text(response, *, phase: str, rag_doc_count: int):
        choices = getattr(response, "choices", None)
        if not choices:
            raise AnalysisIntegrityError("missing choice")
        choice = choices[0]
        finish_reason = _safe_finish_reason(
            getattr(choice, "finish_reason", None)
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            print(
                f"[INFO] LLM usage phase={phase} "
                "prompt="
                f"{_safe_usage_count(getattr(usage, 'prompt_tokens', None))} "
                "completion="
                f"{_safe_usage_count(getattr(usage, 'completion_tokens', None))} "
                "total="
                f"{_safe_usage_count(getattr(usage, 'total_tokens', None))} "
                f"finish={finish_reason} "
                f"rag_docs={rag_doc_count}"
            )
        message = getattr(choice, "message", None)
        result = getattr(message, "content", None)
        if isinstance(result, str):
            # Qwen 등 모델이 출력하는 <think>...</think> 블록 제거
            result = re.sub(
                r"<think>.*?</think>",
                "",
                result,
                flags=re.DOTALL,
            ).strip()
        return result, finish_reason

    def analyze(self, error_summary: str, rag_docs: list = None) -> str:
        """주어진 에러 요약과 RAG 문서를 바탕으로 LLM에 분석을 요청합니다."""
        if not error_summary or not error_summary.strip():
            return "분석할 장애 정보가 없습니다."

        prompt = "당신은 10년 차 쿠버네티스 인프라 아키텍트입니다.\n"
        prompt += "다음 클러스터에서 발생한 오류 정보와 RCA(근본 원인 분석) 결과를 기반으로,\n"
        prompt += "한국어로 명확하게 원인과 해결 방법을 구조화하여 답변해 주세요.\n\n"
        prompt += (
            "전체 답변은 900 토큰 이내로 간결하게 작성하고, 같은 설명이나 명령을 "
            "반복하지 마세요.\n\n"
        )

        prompt += "--- [오류 정보 및 RCA 결과] ---\n"
        prompt += f"{error_summary}\n\n"

        bounded_rag_docs = [
            str(doc).strip()[: self.rag_max_chars_per_doc]
            for doc in (rag_docs or [])[: self.rag_max_docs]
            if str(doc).strip()
        ]

        if bounded_rag_docs:
            prompt += "--- [참고 문서 (RAG Context)] ---\n"
            for doc in bounded_rag_docs:
                prompt += f"{doc}\n"
            prompt += "\n"

        prompt += "답변 형식 (반드시 아래 순서와 마크다운 헤더를 그대로 사용해줘):\n\n"

        prompt += "## 장애 요약\n"
        prompt += "(에러의 핵심을 1줄로 요약)\n\n"

        prompt += "## 근본 원인 (Root Cause)\n"
        prompt += "위 RCA 분석 결과에서 가장 점수가 높은 원인을 아래 형식으로 명시해줘:\n"
        prompt += "- **근본 원인**: (종류/이름)\n"
        prompt += "- **심각도 점수**: (숫자)\n"
        prompt += "- **상태**: (ERROR/WARNING)\n"
        prompt += "- **에러 이유**: (NotReady, Missing 등)\n"
        prompt += "- **인과 체인**: 전파 경로를 화살표(→)로 연결\n"
        prompt += "- **영향 범위**: N개 리소스\n\n"

        prompt += "## 인과 관계 분석\n"
        prompt += "근본 원인이 어떻게 다른 리소스로 전파되었는지 단계별로 설명해줘.\n"
        prompt += "각 단계마다 '왜' 그 영향이 발생하는지 쿠버네티스 내부 동작 원리를 포함해서.\n\n"

        prompt += "## 참고 문서\n"
        prompt += "이 문제와 관련된 Kubernetes 공식 문서 URL을 1~3개 나열.\n"
        prompt += "형식: 각 줄에 `- https://kubernetes.io/docs/...`\n"
        if bounded_rag_docs:
            prompt += "RAG 참고 문서 출처 URL이 있으면 함께 포함시켜줘.\n\n"
        else:
            prompt += "\n\n"

        prompt += "## 해결 방법\n"
        prompt += "근본 원인부터 해결하는 순서로 구체적인 kubectl 명령어와 YAML 패치를 제시해줘.\n"
        prompt += "반드시 '근본 원인 해결 → 중간 계층 확인 → 최종 서비스 복구' 순서로.\n"
        prompt += (
            "실행 가능한 명령은 최대 6개만 제시하고 추측성 명령은 금지합니다. "
            "`kubectl scale service`와 `kubectl scale pod`는 유효하지 않으므로 "
            "절대 제시하지 마세요.\n"
        )

        system_prompt = (
            "당신은 Kubernetes 장애 보고서 작성자입니다.\n"
            "아래 RCA 엔진이 확정한 근본 원인은 절대 변경하지 마세요.\n"
            "당신의 역할은:\n"
            "1. 확정된 근본 원인을 쿠버네티스 내부 동작 원리로 설명\n"
            "2. 전파 경로의 각 단계가 왜 발생하는지 기술적으로 해설\n"
            "3. 근본 원인부터 해결하는 순서로 구체적 명령어 제시\n"
            "절대로 자체적으로 다른 근본 원인을 추론하거나 제시하지 마세요.\n"
            "RCA 엔진의 결과가 '정상'이면 '정상'이라고만 답하세요."
        )

        request_lock = None
        try:
            request_lock = self._take_request_slot()
            budget = self._new_analysis_budget()
            response = self._create_analysis_completion_unlocked(
                budget=budget,
                phase="primary",
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=self.max_output_tokens,
            )
            primary_result, primary_finish = self._completion_text(
                response,
                phase="primary",
                rag_doc_count=len(bounded_rag_docs),
            )
            try:
                return validate_analysis_output(
                    primary_result,
                    primary_finish,
                )
            except AnalysisIntegrityError as integrity_error:
                repair_limit = min(
                    1,
                    max(
                        0,
                        int(getattr(self, "format_repair_max_attempts", 1)),
                    ),
                )
                diagnostics = _heading_repair_diagnostics(
                    primary_result,
                    primary_finish,
                )
                action = (
                    "skip_repair_disabled"
                    if repair_limit == 0
                    else (
                        "repair"
                        if diagnostics.candidate
                        else "skip_not_candidate"
                    )
                )
                _log_integrity_diagnostics(
                    phase="primary",
                    error=integrity_error,
                    diagnostics=diagnostics,
                    budget=budget,
                    repair_limit=repair_limit,
                    action=action,
                )
                if repair_limit == 0 or not diagnostics.candidate:
                    raise

            self._ensure_budget_for_additional_call(
                budget,
                stage="format repair",
            )
            repair_response = self._create_analysis_completion_unlocked(
                budget=budget,
                phase="repair",
                model=self.model,
                messages=self._repair_messages(primary_result),
                temperature=0,
                max_tokens=self.max_output_tokens,
            )
            repaired_result, repaired_finish = self._completion_text(
                repair_response,
                phase="repair",
                rag_doc_count=0,
            )
            try:
                validated_repair = validate_analysis_output(
                    repaired_result,
                    repaired_finish,
                )
            except AnalysisIntegrityError as integrity_error:
                _log_integrity_diagnostics(
                    phase="repair",
                    error=integrity_error,
                    diagnostics=_heading_repair_diagnostics(
                        repaired_result,
                        repaired_finish,
                    ),
                    budget=budget,
                    repair_limit=repair_limit,
                    action="reject",
                )
                raise
            if not _repair_preserves_semantics(
                primary_result,
                validated_repair,
            ):
                integrity_error = AnalysisIntegrityError(
                    "repair_semantic_drift"
                )
                _log_integrity_diagnostics(
                    phase="repair",
                    error=integrity_error,
                    diagnostics=_heading_repair_diagnostics(
                        validated_repair,
                        repaired_finish,
                    ),
                    budget=budget,
                    repair_limit=repair_limit,
                    action="reject",
                )
                raise integrity_error
            return validated_repair
        except Exception as e:
            if isinstance(e, ProviderRequestError):
                if e.code in {"connection_failed", "timeout"}:
                    raise Exception(
                        "LLM 서버 연결 실패: provider unavailable"
                    ) from None
                if e.code == "model_not_found":
                    raise Exception(
                        f"모델 '{self.model}'을 찾을 수 없습니다. "
                        "서버에 로드된 모델명을 확인하세요."
                    ) from None
                if e.code == "authentication_failed":
                    raise Exception(
                        "LLM 인증 실패: API credential을 확인하세요."
                    ) from None
                if e.code == "quota_exhausted":
                    raise Exception("LLM quota_exhausted") from None
                raise Exception(
                    f"LLM API 오류: provider_{e.code}"
                ) from None
            safe_error = redact_provider_error(str(e))
            err_str = safe_error.lower()
            if any(marker in err_str for marker in ("connection", "refused", "timeout", "timed out")):
                safe_base_url = redact_provider_error(str(self.client.base_url))
                raise Exception(f"LLM 서버 연결 실패: {safe_base_url} — 서버가 실행 중인지 확인하세요.")
            if "404" in err_str:
                raise Exception(f"모델 '{self.model}'을 찾을 수 없습니다. 서버에 로드된 모델명을 확인하세요.")
            if _is_quota_error(e, safe_error):
                raise Exception(f"LLM quota_exhausted: {safe_error}")
            raise Exception(f"LLM API 오류: {safe_error}")
        finally:
            if request_lock is not None:
                request_lock.release()


def redact_provider_error(value: str) -> str:
    """Provider 오류에서 credential과 운영 식별자를 로그 전에 제거한다."""
    value = str(value)
    value = re.sub(
        r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@",
        r"\1[redacted]@",
        value,
    )
    value = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|key)=)[^&\s]+",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)((?:authorization\s*:\s*)?bearer\s+)"
        r"[A-Za-z0-9._~+/=-]+",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)([\"']?(?:organization|project|request[_ -]?id)[\"']?"
        r"\s*[:=]\s*)[\"']?[^\"'\s,}]+[\"']?",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)\b(organization|project|request(?:[_ -]?id)?)\s+"
        r"[`\"']?[A-Za-z0-9][A-Za-z0-9._:-]*[`\"']?",
        r"\1 [redacted]",
        value,
    )
    return re.sub(
        r"\b(?:gsk_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|"
        r"AIza[A-Za-z0-9_-]{16,})\b",
        "[redacted-key]",
        value,
    )


def _safe_finish_reason(value) -> str:
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    if normalized in _ALLOWED_FINISH_REASONS:
        return normalized
    return "unknown"


def _safe_usage_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "N/A"
    return value


def _provider_status_code(exc: Exception):
    candidates = [getattr(exc, "status_code", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        candidates.append(getattr(response, "status_code", None))
    for candidate in candidates:
        try:
            status_code = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status_code <= 599:
            return status_code
    return None


def _sanitized_provider_error(
    exc: Exception,
    *,
    safe_error: str,
    is_quota_error: bool,
) -> ProviderRequestError:
    status_code = _provider_status_code(exc)
    if is_quota_error:
        code = "quota_exhausted"
    elif status_code in {401, 403}:
        code = "authentication_failed"
    elif status_code == 404:
        code = "model_not_found"
    else:
        type_name = type(exc).__name__.lower()
        safe_lower = safe_error.lower()
        if "timeout" in type_name or any(
            marker in safe_lower
            for marker in ("timeout", "timed out")
        ):
            code = "timeout"
        elif "connection" in type_name or any(
            marker in safe_lower
            for marker in ("connection", "refused")
        ):
            code = "connection_failed"
        elif status_code is not None:
            code = "http_error"
        else:
            code = "request_failed"
    return ProviderRequestError(code, status_code)


def _line_without_ending(line: str):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _commonmark_line_content(line: str):
    value = line.lstrip("\ufeff") if line.startswith("\ufeff") else line
    if value.startswith("\t"):
        return None
    leading_spaces = len(value) - len(value.lstrip(" "))
    if leading_spaces > 3:
        return None
    content = value[leading_spaces:]
    if content.startswith("\t"):
        return None
    return content


def _fence_marker(line: str):
    content = _commonmark_line_content(line)
    if content is None:
        return None
    match = re.match(r"^(`{3,}|~{3,})", content)
    if match is None:
        return None
    marker = match.group(1)
    remainder = content[len(marker):]
    if marker[0] == "`" and "`" in remainder:
        return None
    return marker[0], len(marker)


def _is_closing_fence(line: str, active_fence) -> bool:
    content = _commonmark_line_content(line)
    if content is None:
        return False
    marker_char, minimum_length = active_fence
    match = re.match(
        rf"^({re.escape(marker_char)}{{{minimum_length},}})(.*)$",
        content,
    )
    return match is not None and not match.group(2).strip()


def _strip_full_emphasis(value: str) -> str:
    value = value.strip()
    for marker in ("**", "__"):
        if (
            value.startswith(marker)
            and value.endswith(marker)
            and len(value) > len(marker) * 2
        ):
            return value[len(marker):-len(marker)].strip()
    return value


def _canonical_heading_alias(line: str):
    candidate = _commonmark_line_content(line)
    if candidate is None:
        return None
    candidate = candidate.rstrip()
    atx = re.match(r"^(#{1,6})(?:[ \t]+|$)(.*)$", candidate)
    if atx is not None:
        candidate = atx.group(2).strip()
        candidate = re.sub(r"\s+#{1,6}\s*$", "", candidate).strip()
    else:
        emphasized = _strip_full_emphasis(candidate)
        if emphasized == candidate:
            return None
        candidate = emphasized

    candidate = re.sub(r"[\s:：]+$", "", candidate).strip()
    candidate = _strip_full_emphasis(candidate)
    candidate = re.sub(
        r"^\d{1,2}\s*[\.\)]\s*",
        "",
        candidate,
    ).strip()
    candidate = _strip_full_emphasis(candidate)
    candidate = re.sub(r"[\s:：]+$", "", candidate).strip()
    candidate = re.sub(r"\s+", " ", candidate)

    explicit_aliases = (
        (
            r"장애 요약"
            r"(?:\s*\(\s*incident\s+summary\s*\))?",
            REQUIRED_ANALYSIS_HEADINGS[0],
        ),
        (
            r"근본 원인(?:\s+분석)?"
            r"(?:\s*\(\s*root\s+cause(?:\s+analysis)?\s*\))?",
            REQUIRED_ANALYSIS_HEADINGS[1],
        ),
        (
            r"인과 관계 분석"
            r"(?:\s*\(\s*(?:causal(?:\s+relationship)?|causality)"
            r"\s+analysis\s*\))?",
            REQUIRED_ANALYSIS_HEADINGS[2],
        ),
        (
            r"참고 문서"
            r"(?:\s*\(\s*(?:official\s+)?references?\s*\))?",
            REQUIRED_ANALYSIS_HEADINGS[3],
        ),
        (
            r"해결 방법"
            r"(?:\s*\(\s*(?:resolution|remediation|solution)"
            r"(?:\s+(?:steps?|methods?))?\s*\))?",
            REQUIRED_ANALYSIS_HEADINGS[4],
        ),
    )
    for alias_pattern, canonical in explicit_aliases:
        if re.fullmatch(
            alias_pattern,
            candidate,
            flags=re.IGNORECASE,
        ):
            return canonical
    return None


def normalize_analysis_headings(result: str) -> str:
    """명시적 section heading alias만 code fence 밖에서 canonical H2로 바꾼다."""
    if not isinstance(result, str):
        return result

    normalized_lines = []
    active_fence = None
    for line in result.splitlines(keepends=True):
        content, ending = _line_without_ending(line)
        if active_fence is not None:
            normalized_lines.append(line)
            if _is_closing_fence(content, active_fence):
                active_fence = None
            continue
        marker = _fence_marker(content)
        if marker is not None:
            active_fence = marker
            normalized_lines.append(line)
            continue

        canonical = _canonical_heading_alias(content)
        if canonical is None:
            normalized_lines.append(line)
        else:
            normalized_lines.append(canonical + ending)
    return "".join(normalized_lines).strip()


def _analysis_structure(result: str):
    lines = result.splitlines()
    active_fence = None
    positions = []
    balanced = True
    for index, line in enumerate(lines):
        if active_fence is not None:
            if _is_closing_fence(line, active_fence):
                active_fence = None
            continue
        marker = _fence_marker(line)
        if marker is not None:
            active_fence = marker
            continue
        content = _commonmark_line_content(line)
        if content is not None and content.rstrip() in REQUIRED_ANALYSIS_HEADINGS:
            positions.append((index, content.rstrip()))
    if active_fence is not None:
        balanced = False
    return lines, positions, balanced


def _unexpected_heading_count(result: str) -> int:
    count = 0
    active_fence = None
    previous_content = None
    for line in result.splitlines():
        if active_fence is not None:
            if _is_closing_fence(line, active_fence):
                active_fence = None
            previous_content = None
            continue
        marker = _fence_marker(line)
        if marker is not None:
            active_fence = marker
            previous_content = None
            continue
        content = _commonmark_line_content(line)
        if content is None:
            previous_content = None
            continue
        if re.fullmatch(r"(?:=+|-+)[ \t]*", content):
            if previous_content is not None and previous_content.strip():
                count += 1
            previous_content = None
            continue
        if re.match(r"^#{1,6}(?:[ \t]+|$)", content) is not None:
            if content.rstrip() not in REQUIRED_ANALYSIS_HEADINGS:
                count += 1
            previous_content = None
            continue
        previous_content = content if content.strip() else None
    return count


def _reference_section_text(result: str) -> str:
    lines, positions, _ = _analysis_structure(result)
    for index, (line_index, heading) in enumerate(positions):
        if heading != REQUIRED_ANALYSIS_HEADINGS[3]:
            continue
        next_index = (
            positions[index + 1][0]
            if index + 1 < len(positions)
            else len(lines)
        )
        return "\n".join(lines[line_index + 1:next_index])
    return ""


def _extracted_urls(result: str):
    for match in re.finditer(
        r"https?://[^\s<>{}\[\]()`\"']+",
        result,
        flags=re.IGNORECASE,
    ):
        yield match.group(0).rstrip(".,;:!?")


def _is_official_kubernetes_docs_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "kubernetes.io"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False
    return bool(
        re.match(
            r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?docs/",
            parsed.path,
            flags=re.IGNORECASE,
        )
    )


def _contains_official_kubernetes_url(
    result: str,
    *,
    reference_section_only: bool = False,
) -> bool:
    search_text = (
        _reference_section_text(result)
        if reference_section_only
        else result
    )
    return any(
        _is_official_kubernetes_docs_url(url)
        for url in _extracted_urls(search_text)
    )


def _has_invalid_url(result: str) -> bool:
    return any(
        not _is_official_kubernetes_docs_url(url)
        for url in _extracted_urls(result)
    )


def _reference_urls_are_valid(result: str) -> bool:
    urls = tuple(_extracted_urls(_reference_section_text(result)))
    return (
        1 <= len(urls) <= 3
        and all(_is_official_kubernetes_docs_url(url) for url in urls)
    )


def _contains_unsupported_scale_command(result: str) -> bool:
    option_values = {
        "-n",
        "-l",
        "--namespace",
        "--context",
        "--kubeconfig",
        "--cluster",
        "--user",
        "--server",
        "--token",
        "--as",
        "--as-group",
        "--request-timeout",
        "--cache-dir",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--tls-server-name",
        "--replicas",
        "--current-replicas",
        "--resource-version",
        "--timeout",
        "--selector",
        "-o",
        "--output",
        "--dry-run",
        "--field-manager",
        "--subresource",
        "--template",
        "-f",
        "--filename",
        "-k",
        "--kustomize",
    }
    forbidden_resources = {
        "pod",
        "pods",
        "po",
        "service",
        "services",
        "svc",
    }
    logical_text = re.sub(r"\\\r?\n", " ", result)
    for segment in re.split(r"(?:\r?\n|&&|\|\||;)", logical_text):
        try:
            tokens = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            tokens = re.findall(r"[^\s]+", segment)
        normalized_tokens = [
            token.strip("`").lstrip("$").lower()
            for token in tokens
        ]
        for kubectl_index, token in enumerate(normalized_tokens):
            if token.rsplit("/", 1)[-1] != "kubectl":
                continue
            try:
                scale_index = normalized_tokens.index(
                    "scale",
                    kubectl_index + 1,
                )
            except ValueError:
                continue
            index = scale_index + 1
            while index < len(normalized_tokens):
                target = normalized_tokens[index].strip(",")
                if not target:
                    index += 1
                    continue
                if target == "--":
                    index += 1
                    continue
                if target.startswith("-"):
                    option_name = target.split("=", 1)[0]
                    if (
                        "=" not in target
                        and option_name in option_values
                        and index + 1 < len(normalized_tokens)
                    ):
                        index += 2
                    else:
                        index += 1
                    continue
                resource = target.split("/", 1)[0].split(".", 1)[0]
                if resource in forbidden_resources:
                    return True
                if "/" not in target:
                    break
                index += 1
    return False


def _heading_repair_diagnostics(
    result: str,
    finish_reason: str,
) -> _HeadingRepairDiagnostics:
    finish_reason = _safe_finish_reason(finish_reason)
    non_empty = isinstance(result, str) and bool(result.strip())
    if non_empty:
        normalized = normalize_analysis_headings(result)
        _, positions, balanced = _analysis_structure(normalized)
        has_official_url = _contains_official_kubernetes_url(normalized)
        has_invalid_url = _has_invalid_url(normalized)
        has_unsupported_scale = _contains_unsupported_scale_command(
            normalized
        )
        unexpected_heading_count = _unexpected_heading_count(normalized)
    else:
        positions = []
        balanced = True
        has_official_url = False
        has_invalid_url = False
        has_unsupported_scale = False
        unexpected_heading_count = 0

    observed = [heading for _, heading in positions]
    distinct_observed = set(observed)
    present_count = len(distinct_observed)
    missing_headings = tuple(
        heading
        for heading in REQUIRED_ANALYSIS_HEADINGS
        if heading not in distinct_observed
    )
    missing_count = len(missing_headings)
    has_duplicates = len(observed) != present_count
    expected_ranks = {
        heading: index
        for index, heading in enumerate(REQUIRED_ANALYSIS_HEADINGS)
    }
    ranks = [expected_ranks[heading] for heading in observed]
    valid_order = ranks == sorted(ranks)

    candidate = False
    if finish_reason != "stop":
        reason = "finish_reason_not_stop"
    elif not non_empty:
        reason = "empty_response"
    elif not balanced:
        reason = "unbalanced_code_fence"
    elif not has_official_url:
        reason = "missing_official_url"
    elif has_invalid_url:
        reason = "invalid_url"
    elif has_unsupported_scale:
        reason = "unsupported_scale_command"
    elif unexpected_heading_count:
        reason = "unexpected_heading_structure"
    elif present_count < 3:
        reason = "too_few_recognized_headings"
    elif has_duplicates:
        reason = "duplicate_headings"
    elif not valid_order:
        reason = "invalid_heading_order"
    elif missing_count != 1:
        reason = "missing_heading_count_out_of_repair_range"
    elif missing_headings != (REQUIRED_ANALYSIS_HEADINGS[3],):
        reason = "unsupported_missing_heading"
    else:
        candidate = True
        reason = "eligible"

    return _HeadingRepairDiagnostics(
        candidate=candidate,
        reason=reason,
        present_heading_count=present_count,
        missing_heading_count=missing_count,
        balanced_code_fence=balanced,
        has_official_url=has_official_url,
        has_invalid_url=has_invalid_url,
        has_unsupported_scale_command=has_unsupported_scale,
        has_duplicate_headings=has_duplicates,
        has_valid_heading_order=valid_order,
        unexpected_heading_count=unexpected_heading_count,
    )


def _log_integrity_diagnostics(
    *,
    phase: str,
    error: AnalysisIntegrityError,
    diagnostics: _HeadingRepairDiagnostics,
    budget: _AnalysisRequestBudget,
    repair_limit: int,
    action: str,
) -> None:
    remaining_wall = max(0, int(budget.deadline - time.monotonic()))
    print(
        "[WARN] LLM integrity "
        f"phase={phase} "
        f"code={error.code} "
        f"repair_candidate={str(diagnostics.candidate).lower()} "
        f"reason={diagnostics.reason} "
        f"present_headings={diagnostics.present_heading_count} "
        f"missing_headings={diagnostics.missing_heading_count} "
        f"balanced_fence={str(diagnostics.balanced_code_fence).lower()} "
        f"official_url={str(diagnostics.has_official_url).lower()} "
        f"invalid_url={str(diagnostics.has_invalid_url).lower()} "
        "unsupported_scale="
        f"{str(diagnostics.has_unsupported_scale_command).lower()} "
        f"duplicate_headings={str(diagnostics.has_duplicate_headings).lower()} "
        f"valid_heading_order="
        f"{str(diagnostics.has_valid_heading_order).lower()} "
        f"unexpected_headings={diagnostics.unexpected_heading_count} "
        f"repair_limit={repair_limit} "
        f"quota_retries_used={budget.quota_retries_used} "
        f"quota_wait_seconds={budget.quota_wait_seconds} "
        f"provider_attempts={budget.provider_attempts} "
        f"wall_remaining_seconds={remaining_wall} "
        f"action={action}"
    )


def validate_analysis_output(result: str, finish_reason: str) -> str:
    """잘리거나 구조·명령 계약을 어긴 troubleshooting을 성공 처리하지 않는다."""
    safe_finish_reason = _safe_finish_reason(finish_reason)
    if safe_finish_reason != "stop":
        raise AnalysisIntegrityError(
            f"finish_reason={safe_finish_reason}"
        )
    if not isinstance(result, str) or not result.strip():
        raise AnalysisIntegrityError("empty response")

    normalized = normalize_analysis_headings(result)
    lines, positions, balanced = _analysis_structure(normalized)
    if not balanced:
        raise AnalysisIntegrityError("unbalanced code fence")
    if _unexpected_heading_count(normalized):
        raise AnalysisIntegrityError("unexpected heading structure")

    counts = {
        heading: sum(
            1 for _, observed in positions if observed == heading
        )
        for heading in REQUIRED_ANALYSIS_HEADINGS
    }
    if any(counts[heading] == 0 for heading in REQUIRED_ANALYSIS_HEADINGS):
        raise AnalysisIntegrityError("missing required headings")
    if any(counts[heading] > 1 for heading in REQUIRED_ANALYSIS_HEADINGS):
        raise AnalysisIntegrityError("duplicate heading structure")

    observed_order = [heading for _, heading in positions]
    if observed_order != list(REQUIRED_ANALYSIS_HEADINGS):
        raise AnalysisIntegrityError("invalid heading order")

    for position_index, (line_index, _) in enumerate(positions):
        next_index = (
            positions[position_index + 1][0]
            if position_index + 1 < len(positions)
            else len(lines)
        )
        if not "\n".join(lines[line_index + 1:next_index]).strip():
            raise AnalysisIntegrityError("empty section body")

    if _has_invalid_url(normalized) or not _reference_urls_are_valid(normalized):
        raise AnalysisIntegrityError(
            "invalid official Kubernetes URL contract"
        )
    if _contains_unsupported_scale_command(normalized):
        raise AnalysisIntegrityError(
            "unsupported kubectl scale target"
        )
    return normalized.strip()


def _is_heading_only_repair_candidate(
    result: str,
    finish_reason: str,
) -> bool:
    return _heading_repair_diagnostics(
        result,
        finish_reason,
    ).candidate


def _semantic_projection(result: str):
    kept_lines = []
    heading_anchors = []
    active_fence = None
    byte_offset = 0
    for line in result.splitlines(keepends=True):
        content, _ = _line_without_ending(line)
        if active_fence is not None:
            kept_lines.append(line)
            byte_offset += len(line.encode("utf-8"))
            if _is_closing_fence(content, active_fence):
                active_fence = None
            continue
        marker = _fence_marker(content)
        if marker is not None:
            active_fence = marker
            kept_lines.append(line)
            byte_offset += len(line.encode("utf-8"))
            continue
        canonical = _canonical_heading_alias(content)
        if canonical is not None:
            heading_anchors.append((canonical, byte_offset))
            continue
        kept_lines.append(line)
        byte_offset += len(line.encode("utf-8"))
    return "".join(kept_lines), tuple(heading_anchors)


def _non_heading_content(result: str) -> str:
    return _semantic_projection(result)[0]


def _normalized_matching_lines(result: str, pattern: str):
    return tuple(
        re.sub(r"\s+", " ", line.strip())
        for line in result.splitlines()
        if re.search(pattern, line, flags=re.IGNORECASE)
    )


def _first_official_url_line_anchor(content: str):
    byte_offset = 0
    for line in content.splitlines(keepends=True):
        line_content, _ = _line_without_ending(line)
        if any(
            _is_official_kubernetes_docs_url(url)
            for url in _extracted_urls(line_content)
        ):
            return byte_offset
        byte_offset += len(line.encode("utf-8"))
    return None


def _repair_preserves_semantics(before: str, after: str) -> bool:
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    before_content, before_anchors = _semantic_projection(before)
    after_content, after_anchors = _semantic_projection(after)
    if hashlib.sha256(before_content.encode("utf-8")).digest() != hashlib.sha256(
        after_content.encode("utf-8")
    ).digest():
        return False
    if before_content != after_content:
        return False

    before_anchor_map = dict(before_anchors)
    after_anchor_map = dict(after_anchors)
    if (
        len(before_anchor_map) != len(before_anchors)
        or len(after_anchor_map) != len(after_anchors)
    ):
        return False
    if any(
        after_anchor_map.get(heading) != offset
        for heading, offset in before_anchors
    ):
        return False
    added_headings = set(after_anchor_map) - set(before_anchor_map)
    reference_heading = REQUIRED_ANALYSIS_HEADINGS[3]
    if added_headings != {reference_heading}:
        return False
    expected_reference_anchor = _first_official_url_line_anchor(before_content)
    if (
        expected_reference_anchor is None
        or after_anchor_map[reference_heading] != expected_reference_anchor
    ):
        return False

    if tuple(_extracted_urls(before_content)) != tuple(
        _extracted_urls(after_content)
    ):
        return False
    if _normalized_matching_lines(
        before_content,
        r"\bkubectl\b",
    ) != _normalized_matching_lines(after_content, r"\bkubectl\b"):
        return False
    return _normalized_matching_lines(
        before_content,
        r"(?:근본 원인|심각도 점수|에러 이유|인과 체인|영향 범위)",
    ) == _normalized_matching_lines(
        after_content,
        r"(?:근본 원인|심각도 점수|에러 이유|인과 체인|영향 범위)",
    )


def parse_retry_after_seconds(value: str) -> int:
    """Provider 메시지의 `try again in 6m31.2s` 형식을 초로 변환한다."""
    match = re.search(
        r"try again in\s*"
        r"(?P<duration>(?:\d+(?:\.\d+)?(?:ms|h|m|s)\s*)+)",
        value or "",
        flags=re.IGNORECASE,
    )
    if match is None:
        return 0
    multipliers = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    total = sum(
        float(amount) * multipliers[unit.lower()]
        for amount, unit in re.findall(
            r"(\d+(?:\.\d+)?)(ms|h|m|s)",
            match.group("duration"),
            flags=re.IGNORECASE,
        )
    )
    return int(math.ceil(total))


def _is_quota_error(exc: Exception, safe_error: str) -> bool:
    """Prefer the provider HTTP status and retain the legacy message fallback."""
    candidates = [getattr(exc, "status_code", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        candidates.append(getattr(response, "status_code", None))
    for status_code in candidates:
        try:
            if int(status_code) == 429:
                return True
        except (TypeError, ValueError):
            continue
    return "429" in safe_error and "rate limit" in safe_error.lower()


def _retry_after_seconds(exc: Exception, safe_error: str) -> int:
    """Read Groq's numeric Retry-After header before parsing the safe body."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    raw_value = None
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            raw_value = getter("retry-after")
            if raw_value is None:
                raw_value = getter("Retry-After")
        if raw_value is None:
            try:
                raw_value = next(
                    value
                    for key, value in headers.items()
                    if str(key).lower() == "retry-after"
                )
            except (AttributeError, StopIteration, TypeError):
                raw_value = None

    try:
        header_seconds = float(raw_value)
    except (TypeError, ValueError):
        header_seconds = 0.0
    if math.isfinite(header_seconds) and header_seconds > 0:
        return int(math.ceil(header_seconds))
    return parse_retry_after_seconds(safe_error)


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


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


if __name__ == "__main__":
    DocLoader().load_local_md_files()
