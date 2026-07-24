"""
intelligence/llm.py
====================
LLM 분석 + RAG 문서 로딩/검색 통합 모듈
"""

import os
import re
from typing import List, Dict

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

    def __init__(self, top_k: int = 3):
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
        return [doc["text"] for doc in self.search(query)]


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
        self.client = OpenAI(
            base_url=base_url or os.environ.get("LLM_BASE_URL", "http://222.105.251.70:30135/v1"),
            api_key=api_key or os.environ.get("LLM_API_KEY", "not-needed"),
        )
        self.model = model or os.environ.get("LLM_MODEL", "qwen3-32b-gpu3-test")

    def analyze(self, error_summary: str, rag_docs: list = None) -> str:
        """주어진 에러 요약과 RAG 문서를 바탕으로 LLM에 분석을 요청합니다."""
        if not error_summary or not error_summary.strip():
            return "분석할 장애 정보가 없습니다."

        prompt = "당신은 10년 차 쿠버네티스 인프라 아키텍트입니다.\n"
        prompt += "다음 클러스터에서 발생한 오류 정보와 RCA(근본 원인 분석) 결과를 기반으로,\n"
        prompt += "한국어로 명확하게 원인과 해결 방법을 구조화하여 답변해 주세요.\n\n"

        prompt += "--- [오류 정보 및 RCA 결과] ---\n"
        prompt += f"{error_summary}\n\n"

        if rag_docs:
            prompt += "--- [참고 문서 (RAG Context)] ---\n"
            for doc in rag_docs:
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
        if rag_docs:
            prompt += "RAG 참고 문서 출처 URL이 있으면 함께 포함시켜줘.\n\n"
        else:
            prompt += "\n\n"

        prompt += "## 해결 방법\n"
        prompt += "근본 원인부터 해결하는 순서로 구체적인 kubectl 명령어와 YAML 패치를 제시해줘.\n"
        prompt += "반드시 '근본 원인 해결 → 중간 계층 확인 → 최종 서비스 복구' 순서로.\n"

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

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            result = response.choices[0].message.content
            # Qwen 등 모델이 출력하는 <think>...</think> 블록 제거
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
            return result
        except Exception as e:
            err_str = str(e).lower()
            if "connection" in err_str or "refused" in err_str:
                raise Exception(f"LLM 서버 연결 실패: {self.client.base_url} — 서버가 실행 중인지 확인하세요.")
            if "404" in err_str:
                raise Exception(f"모델 '{self.model}'을 찾을 수 없습니다. 서버에 로드된 모델명을 확인하세요.")
            raise Exception(f"LLM API 오류: {e}")


if __name__ == "__main__":
    DocLoader().load_local_md_files()
