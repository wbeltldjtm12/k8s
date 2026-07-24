"""
main.py
====================
KubeIn 전체 분석 파이프라인 오케스트레이션 및 FastAPI 서비스 제공
"""
import os
import time
import uuid
from datetime import datetime
from threading import Lock

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from infra.config import load_k8s_config
from infra.cache import ResourceCache
from infra.metrics import MetricsAnalyzer
from analysis.analyzers import (
    PodAnalyzer, DeploymentAnalyzer, NodeAnalyzer,
    ServiceAnalyzer, IngressAnalyzer, PVCAnalyzer,
    HPAAnalyzer, JobAnalyzer, CronJobAnalyzer, PVAnalyzer,
)
from analysis.graph import DependencyGraph
from analysis.events import EventAnalyzer
from intelligence.llm import LLMClient, Retriever


# ── 앱 초기화 ────────────────────────────────────────────────────────
app = FastAPI(title="KubeInsight API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

load_k8s_config()

llm = LLMClient()
retriever = Retriever()

analyzers = [
    ("Pod",        PodAnalyzer()),
    ("Node",       NodeAnalyzer()),
    ("Deployment", DeploymentAnalyzer()),
    ("Service",    ServiceAnalyzer()),
    ("PVC",        PVCAnalyzer()),
    ("Ingress",    IngressAnalyzer()),
    ("HPA",        HPAAnalyzer()),
    ("Job",        JobAnalyzer()),
    ("CronJob",    CronJobAnalyzer()),
    ("PV",         PVAnalyzer()),
]


# ── TTL 캐시 ─────────────────────────────────────────────────────────
_cache_lock = Lock()
_cache_store: dict = {
    "cache": None, "graph": None, "results": None, "expires_at": 0.0,
}
CACHE_TTL = 30

# 평가용 불변 관측본은 일반 TTL 캐시와 분리합니다. 일반 캐시 무효화나
# LLM 응답 지연이 3-mode 비교의 입력을 바꾸면 안 됩니다.
_eval_snapshot_lock = Lock()
_eval_snapshots: dict[str, dict] = {}
_expired_eval_snapshot_ids: dict[str, float] = {}
EVAL_SNAPSHOT_TTL = 15 * 60
MAX_EVAL_SNAPSHOTS = 8


def _prune_eval_snapshots_locked(now_monotonic: float) -> None:
    """만료 snapshot을 정리합니다. 호출 시 lock을 잡고 있어야 합니다."""
    for snapshot_id, tombstone_expires_at in list(_expired_eval_snapshot_ids.items()):
        if now_monotonic >= tombstone_expires_at:
            _expired_eval_snapshot_ids.pop(snapshot_id, None)

    expired_ids = [
        snapshot_id
        for snapshot_id, record in _eval_snapshots.items()
        if now_monotonic >= record["expires_at_monotonic"]
    ]
    for snapshot_id in expired_ids:
        record = _eval_snapshots.get(snapshot_id)
        if record is None:
            continue
        record["expired"] = True
        record["delete_requested"] = True
        _expired_eval_snapshot_ids[snapshot_id] = now_monotonic + EVAL_SNAPSHOT_TTL
        if record.get("active_requests", 0) == 0:
            _eval_snapshots.pop(snapshot_id, None)


def _acquire_eval_snapshot(snapshot_id: str):
    """평가 관측본 사용권과 조회 오류(not_found/expired)를 반환합니다."""
    now_monotonic = time.monotonic()
    with _eval_snapshot_lock:
        _prune_eval_snapshots_locked(now_monotonic)
        record = _eval_snapshots.get(snapshot_id)
        if record is None:
            if snapshot_id in _expired_eval_snapshot_ids:
                return None, "expired"
            return None, "not_found"
        if record.get("expired"):
            return None, "expired"
        if record.get("state") != "ready" or record.get("delete_requested"):
            return None, "not_found"
        record["active_requests"] = record.get("active_requests", 0) + 1
        return {
            "cache": record["cache"],
            "graph": record["graph"],
            "captured_at": record["captured_at"],
            "captured_at_iso": record["captured_at_iso"],
        }, None


def _release_eval_snapshot_use(snapshot_id: str) -> None:
    """evaluate 요청이 잡은 snapshot 사용권을 반환합니다."""
    with _eval_snapshot_lock:
        record = _eval_snapshots.get(snapshot_id)
        if record is None:
            return
        record["active_requests"] = max(0, record.get("active_requests", 0) - 1)
        if record["active_requests"] == 0 and record.get("delete_requested"):
            _eval_snapshots.pop(snapshot_id, None)


def _get_or_build_graph():
    """TTL 기반 ResourceCache + DependencyGraph 캐싱."""
    now = time.time()
    with _cache_lock:
        if _cache_store["cache"] and now < _cache_store["expires_at"]:
            return _cache_store["cache"], _cache_store["graph"], _cache_store["results"]

    t0 = time.time()
    cache = ResourceCache()
    t1 = time.time()
    print(f"[INFO] ResourceCache (K8s API 17회 호출): {t1 - t0:.1f}초")

    graph = DependencyGraph(cache)
    t2 = time.time()
    print(f"[INFO] DependencyGraph 구축: {t2 - t1:.1f}초")

    results = graph.find_root_causes()
    t3 = time.time()
    print(f"[INFO] RCA 탐색: {t3 - t2:.1f}초")

    with _cache_lock:
        _cache_store.update({
            "cache": cache, "graph": graph,
            "results": results, "expires_at": time.time() + CACHE_TTL,
        })

    return cache, graph, results


# ── 공통 헬퍼 ────────────────────────────────────────────────────────

def _build_rca_context(root_causes) -> str:
    """RCA 결과를 LLM 프롬프트에 넣을 텍스트로 변환."""
    if not root_causes:
        return ""

    rca_text = "\n\n--- [Root Cause Analysis 결과 (그래프 알고리즘)] ---\n"
    rca_text += "아래는 의존성 그래프 DFS+BFS 분석으로 확정한 근본 원인입니다.\n"
    rca_text += "이 결과를 기반으로 장애 요약과 해결 방법을 작성해주세요.\n\n"

    for i, rc in enumerate(root_causes[:5], 1):
        rca_text += (
            f"[근본원인 #{i}] (심각도 점수: {rc.score})\n"
            f"  종류: {rc.root_cause_kind}\n"
            f"  이름: {rc.root_cause_name}\n"
            f"  네임스페이스: {rc.root_cause_namespace or '(클러스터 스코프)'}\n"
            f"  상태: {rc.root_cause_status}\n"
            f"  에러 이유: {rc.root_cause_reason}\n"
            f"  전파 경로: {rc.chain_summary}\n"
            f"  영향 범위: {rc.blast_radius}개 리소스에 영향\n\n"
        )

    return rca_text


def _build_rag_query(root_causes) -> str:
    """에러 키워드 기반 RAG 쿼리 생성 (전체 컨텍스트 대신 핵심 키워드만 사용)."""
    if not root_causes:
        return "Kubernetes cluster healthy no errors"
    parts = []
    for rc in root_causes[:3]:
        parts.append(f"Kubernetes {rc.root_cause_kind} {rc.root_cause_status} {rc.root_cause_reason}")
    return " ".join(parts)


def _build_graph_payload(graph, results):
    """graph_data + chains_data 딕셔너리를 반환하는 공통 헬퍼."""
    STATUS_COLOR = {"ERROR": "#ef4444", "WARNING": "#f97316", "OK": "#22c55e"}
    MISSING_COLOR = "#1f2937"

    top_results = results[:5]

    chains_data = []
    for r in top_results:
        rc = r.root_cause
        chains_data.append({
            "root_cause":           rc.name,
            "root_cause_kind":      rc.kind,
            "root_cause_namespace": rc.namespace,
            "root_cause_reason":    rc.error_reason,
            "root_cause_message":   rc.error_message,
            "root_cause_priority":  rc.priority,
            "is_system":            rc.is_system,
            "system_tag":           " [시스템 컴포넌트]" if rc.is_system else "",
            "chain_summary":        r.build_summary(),
            "depth":                r.depth,
            "blast_radius":         r.blast_radius,
            "score":                round(r.score, 2),
            "affected_node_ids":    [n.id for n in r.affected_nodes],
        })

    graph_nodes, seen = [], set()
    for node in graph.nodes.values():
        nid = node.id
        if nid in seen:
            continue
        seen.add(nid)
        color = MISSING_COLOR if "Missing" in node.error_reason else STATUS_COLOR.get(node.status, "#6b7280")
        graph_nodes.append({
            "id": nid, "label": f"{node.kind}\n{node.name}",
            "kind": node.kind, "status": node.status, "color": color,
            "error_reason": node.error_reason, "namespace": node.namespace,
        })

    graph_edges = []
    for src_key, dst_keys in graph.forward_edges.items():
        src = graph.nodes.get(src_key)
        if not src:
            continue
        for dst_key in dst_keys:
            dst = graph.nodes.get(dst_key)
            if dst:
                graph_edges.append({"source": src.id, "target": dst.id, "label": ""})

    dfs_path = []
    for result in top_results:
        chain = result.chain
        for i in range(len(chain) - 1):
            a_id, b_id = chain[i].id, chain[i + 1].id
            dfs_path.append({"source": a_id, "target": b_id})
            dfs_path.append({"source": b_id, "target": a_id})

    return {
        "total_chains": len(results),
        "chains": chains_data,
        "graph_data": {"nodes": graph_nodes, "edges": graph_edges, "dfs_path": dfs_path},
    }


# ── 엔드포인트 ────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    """서버 상태 확인 엔드포인트."""
    return {"status": "ok", "message": "KubeInsight 엔진이 실행 중입니다."}


@app.post("/api/cache/invalidate")
def invalidate_cache():
    """캐시를 수동 무효화합니다."""
    with _cache_lock:
        _cache_store["expires_at"] = 0
    return {"status": "ok", "message": "캐시가 무효화되었습니다."}


@app.get("/api/analyze")
def analyze_cluster():
    """
    10종 Analyzer로 클러스터를 스캔하고,
    장애를 모아서 RAG + LLM 1회 배치 호출로 분석합니다.
    """
    try:
        analysis_timestamp = time.time()
        timestamp_str = datetime.utcfromtimestamp(analysis_timestamp).isoformat() + "Z"
        print(f"[INFO] /api/analyze 분석 기준 시점: {timestamp_str}")

        cache, _, root_causes = _get_or_build_graph()

        raw_failures = []
        for kind, analyzer in analyzers:
            try:
                results = analyzer.analyze_from_cache(cache)
                for f in results:
                    raw_failures.append({"kind": kind, "failure": f})
            except Exception as e:
                print(f"[WARN] {kind}Analyzer 오류: {e}")

        if not raw_failures:
            return {
                "status": "success",
                "message": "클러스터에서 감지된 이상 상태가 없습니다.",
                "data": [],
            }

        summaries = []
        parsed_data = []
        for item in raw_failures:
            kind = item["kind"]
            failure = item["failure"]
            resource_name = (
                getattr(failure, "name", "")
                or getattr(failure, "pod_name", "")
                or getattr(failure, "node_name", "")
            )
            error_summary = (
                f"리소스 종류: {kind}\n"
                f"이름: {resource_name}\n"
                f"네임스페이스: {getattr(failure, 'namespace', 'N/A')}\n"
                f"에러 이유: {failure.reason}\n"
                f"에러 내용: {failure.message}\n"
            )
            summaries.append(error_summary)
            parsed_data.append({
                "kind": kind,
                "resource": resource_name or "Unknown",
                "namespace": getattr(failure, "namespace", "N/A"),
                "reason": failure.reason,
                "error": failure.message,
            })

        # RCA 결과를 LLM 컨텍스트에 포함
        error_summary_text = "\n---\n".join(summaries[:10])
        rca_summary = _build_rca_context(root_causes)

        try:
            metrics_analyzer = MetricsAnalyzer()
            metrics_context = metrics_analyzer.build_metrics_context_for_llm(
                reference_time=analysis_timestamp
            )
        except Exception as e:
            print(f"[WARN] 메트릭 수집 실패, 메트릭 없이 진행: {e}")
            metrics_context = "\n--- [Prometheus 메트릭] ---\n  메트릭 수집 실패\n"

        timestamp_header = f"[분석 기준 시점: {timestamp_str}]\n\n"
        full_context = timestamp_header + error_summary_text + rca_summary + metrics_context

        # 에러 키워드 기반 RAG 쿼리
        rag_query = _build_rag_query(root_causes)
        rag_texts = retriever.search_texts_only(rag_query)
        ai_analysis = llm.analyze(full_context, rag_texts)

        for item in parsed_data:
            item["analysis"] = ai_analysis
            item["rag_sources"] = []

        return {"status": "success", "data": parsed_data}

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/causality_fast")
def causality_fast():
    """LLM 없이 그래프 + RCA 결과만 즉시 반환합니다."""
    try:
        t0 = time.time()
        _, graph, results = _get_or_build_graph()
        print(f"[INFO] causality_fast 전체: {time.time() - t0:.1f}초")

        if not results:
            return {
                "status": "success",
                "data": {
                    "total_chains": 0, "chains": [], "ai_analysis": "",
                    "graph_data": {"nodes": [], "edges": [], "dfs_path": []},
                },
            }

        payload = _build_graph_payload(graph, results)
        payload["ai_analysis"] = ""
        return {"status": "success", "data": payload}

    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/ai_analysis")
def ai_analysis_endpoint(body: dict):
    """Root Cause 체인 요약을 받아 LLM + RAG 분석 결과를 반환합니다."""
    try:
        summaries = body.get("summaries", "")
        t0 = time.time()
        rag_texts = retriever.search_texts_only(summaries[:2000])
        ai_analysis = llm.analyze(summaries, rag_texts)
        print(f"[INFO] ai_analysis (RAG+LLM): {time.time() - t0:.1f}초")
        return {"status": "success", "analysis": ai_analysis}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.get("/api/health")
def health_check():
    """Kubernetes 클러스터 연결 및 LLM 서버 상태를 확인합니다."""
    results = {}

    try:
        from kubernetes import client as k8s_client
        v1 = k8s_client.CoreV1Api()
        nodes = v1.list_node()
        results["kubernetes"] = {"status": "connected", "nodes": len(nodes.items)}
    except Exception as e:
        results["kubernetes"] = {"status": "error", "message": str(e)}

    results["llm"] = {
        "status": "configured",
        "base_url": os.environ.get("LLM_BASE_URL", "http://222.105.251.70:30135/v1"),
        "model": os.environ.get("LLM_MODEL", "qwen3-32b-gpu3-test"),
    }

    results["chromadb"] = {
        "status": "ready" if retriever._ready else "empty",
        "chunks": retriever.collection.count() if retriever._ready else 0,
    }

    return results


@app.post("/api/evaluate/snapshots")
def create_evaluation_snapshot():
    """3-mode 평가가 공유할 ResourceCache/DependencyGraph 관측본을 생성합니다."""
    snapshot_id = uuid.uuid4().hex
    captured_at = time.time()
    captured_at_iso = datetime.utcfromtimestamp(captured_at).isoformat() + "Z"

    with _eval_snapshot_lock:
        now_monotonic = time.monotonic()
        _prune_eval_snapshots_locked(now_monotonic)
        if len(_eval_snapshots) >= MAX_EVAL_SNAPSHOTS:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "message": "활성 평가 snapshot 한도에 도달했습니다.",
                },
            )
        # 느린 K8s 수집 중에도 동시 생성 한도를 넘지 않도록 slot을 예약합니다.
        _eval_snapshots[snapshot_id] = {
            "state": "building",
            "expires_at_monotonic": now_monotonic + EVAL_SNAPSHOT_TTL,
            "active_requests": 0,
            "delete_requested": False,
            "expired": False,
        }

    try:
        cache = ResourceCache()
        collection_failures = getattr(cache, "collection_failure_count", 0)
        if collection_failures:
            raise RuntimeError(
                f"K8s 리소스 부분 수집 실패: {collection_failures}/17 API calls"
            )
        graph = DependencyGraph(cache)
        error_node_count = len([
            node
            for node in graph.nodes.values()
            if node.status in ("ERROR", "WARNING") and not node.is_system
        ])

        with _eval_snapshot_lock:
            now_monotonic = time.monotonic()
            _prune_eval_snapshots_locked(now_monotonic)
            reservation = _eval_snapshots.get(snapshot_id)
            if (
                reservation is None
                or reservation.get("state") != "building"
                or reservation.get("delete_requested")
            ):
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "error",
                        "message": "평가 snapshot 예약이 만료되거나 취소되었습니다.",
                    },
                )
            reservation.update({
                "state": "ready",
                "cache": cache,
                "graph": graph,
                "captured_at": captured_at,
                "captured_at_iso": captured_at_iso,
                "expires_at_monotonic": now_monotonic + EVAL_SNAPSHOT_TTL,
            })

        return {
            "status": "success",
            "snapshot_id": snapshot_id,
            "captured_at": captured_at_iso,
            "expires_in_sec": EVAL_SNAPSHOT_TTL,
            "error_node_count": error_node_count,
        }
    except Exception as e:
        with _eval_snapshot_lock:
            _eval_snapshots.pop(snapshot_id, None)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


@app.delete("/api/evaluate/snapshots/{snapshot_id}")
def release_evaluation_snapshot(snapshot_id: str):
    """평가 관측본을 해제합니다. 이미 없더라도 성공하는 idempotent API입니다."""
    with _eval_snapshot_lock:
        _prune_eval_snapshots_locked(time.monotonic())
        record = _eval_snapshots.get(snapshot_id)
        released = record is not None
        if record is not None:
            record["delete_requested"] = True
            if record.get("active_requests", 0) == 0:
                _eval_snapshots.pop(snapshot_id, None)
    return {"status": "ok", "released": released}


@app.get("/api/evaluate")
def evaluate(mode: str = "hybrid", snapshot_id: str = ""):
    """
    통합 평가 엔드포인트.
    mode: hybrid (DFS+BFS), dfs_only (DFS만), llm_only (LLM 추론)
    snapshot_id: 지정 시 세 mode가 같은 평가 관측본을 사용
    """
    acquired_snapshot_id = ""
    try:
        t0 = time.time()
        if mode not in {"hybrid", "dfs_only", "llm_only"}:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": f"Unknown mode: {mode}"},
            )

        snapshot_id = snapshot_id.strip()
        snapshot_captured_at = ""
        analysis_timestamp = t0

        if snapshot_id:
            snapshot, lookup_error = _acquire_eval_snapshot(snapshot_id)
            if lookup_error:
                status_code = 410 if lookup_error == "expired" else 404
                message = (
                    "평가 snapshot이 만료되었습니다."
                    if lookup_error == "expired"
                    else "평가 snapshot을 찾을 수 없습니다."
                )
                return JSONResponse(
                    status_code=status_code,
                    content={"status": "error", "message": message},
                )
            acquired_snapshot_id = snapshot_id
            cache = snapshot["cache"]
            graph = snapshot["graph"]
            analysis_timestamp = snapshot["captured_at"]
            snapshot_captured_at = snapshot["captured_at_iso"]
        else:
            now = time.time()
            with _cache_lock:
                if _cache_store["cache"] and now < _cache_store["expires_at"]:
                    cache = _cache_store["cache"]
                    graph = _cache_store["graph"]
                else:
                    cache = None

            if cache is None:
                cache = ResourceCache()
                graph = DependencyGraph(cache)
                with _cache_lock:
                    _cache_store.update({
                        "cache": cache, "graph": graph,
                        "results": None, "expires_at": time.time() + CACHE_TTL,
                    })

        timestamp_str = datetime.utcfromtimestamp(analysis_timestamp).isoformat() + "Z"
        print(f"[INFO] /api/evaluate [{mode}] 분석 기준 시점: {timestamp_str}")

        error_node_count = len([
            n for n in graph.nodes.values()
            if n.status in ("ERROR", "WARNING") and not n.is_system
        ])

        if mode == "hybrid":
            results = graph.find_root_causes()
            payload = _build_graph_payload(graph, results)
            payload["ai_status"] = "not_applicable"
            try:
                error_summary = graph.get_error_summary_for_llm()
                if error_summary != "정상 상태 (이상 노드 없음)":
                    rca_summary = _build_rca_context(results)
                    try:
                        metrics_analyzer = MetricsAnalyzer()
                        metrics_context = metrics_analyzer.build_metrics_context_for_llm(
                            reference_time=analysis_timestamp
                        )
                    except Exception as e:
                        print(f"[WARN] 메트릭 수집 실패, 메트릭 없이 진행: {e}")
                        metrics_context = "\n--- [Prometheus 메트릭] ---\n  메트릭 수집 실패\n"
                    timestamp_header = f"[분석 기준 시점: {timestamp_str}]\n\n"
                    full_context = timestamp_header + error_summary + rca_summary + metrics_context
                    rag_query = _build_rag_query(results)
                    rag_texts = retriever.search_texts_only(rag_query)
                    payload["ai_analysis"] = llm.analyze(full_context, rag_texts)
                    if not isinstance(payload["ai_analysis"], str) or not payload["ai_analysis"].strip():
                        raise RuntimeError("LLM returned an empty analysis")
                    payload["ai_status"] = "success"
                else:
                    payload["ai_analysis"] = ""
            except Exception as e:
                print(f"[WARN] hybrid ai_analysis 실패: {e}")
                payload["ai_analysis"] = ""
                payload["ai_status"] = "error"
            elapsed = round(time.time() - t0, 3)
            response = {
                "status": "success",
                "mode": "hybrid",
                "timing_sec": elapsed,
                "error_node_count": error_node_count,
                "data": payload,
            }
            if snapshot_id:
                response.update({
                    "snapshot_id": snapshot_id,
                    "snapshot_captured_at": snapshot_captured_at,
                })
            return response

        elif mode == "dfs_only":
            results = graph.find_root_causes_dfs_only()
            payload = _build_graph_payload(graph, results)
            payload["ai_analysis"] = ""
            elapsed = round(time.time() - t0, 3)
            response = {
                "status": "success",
                "mode": "dfs_only",
                "timing_sec": elapsed,
                "error_node_count": error_node_count,
                "data": payload,
            }
            if snapshot_id:
                response.update({
                    "snapshot_id": snapshot_id,
                    "snapshot_captured_at": snapshot_captured_at,
                })
            return response

        elif mode == "llm_only":
            error_summary = graph.get_error_summary_for_llm()
            ai_analysis = ""
            ai_status = "not_applicable"
            if error_summary != "정상 상태 (이상 노드 없음)":
                try:
                    rag_query = _build_rag_query([])
                    rag_texts = retriever.search_texts_only(rag_query)
                    ai_analysis = llm.analyze(error_summary, rag_texts)
                    if not isinstance(ai_analysis, str) or not ai_analysis.strip():
                        raise RuntimeError("LLM returned an empty analysis")
                    ai_status = "success"
                except Exception as e:
                    print(f"[WARN] llm_only ai_analysis 실패: {e}")
                    ai_analysis = ""
                    ai_status = "error"
            elapsed = round(time.time() - t0, 3)
            response = {
                "status": "success",
                "mode": "llm_only",
                "timing_sec": elapsed,
                "error_node_count": error_node_count,
                "data": {
                    "total_chains": 0,
                    "chains": [],
                    "ai_analysis": ai_analysis,
                    "ai_status": ai_status,
                    "error_summary": error_summary,
                    "graph_data": {"nodes": [], "edges": [], "dfs_path": []},
                },
            }
            if snapshot_id:
                response.update({
                    "snapshot_id": snapshot_id,
                    "snapshot_captured_at": snapshot_captured_at,
                })
            return response

    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        if acquired_snapshot_id:
            _release_eval_snapshot_use(acquired_snapshot_id)


# ── CLI 오케스트레이션 함수 ───────────────────────────────────────────
def run_analysis():
    """CLI 모드 전체 분석 파이프라인."""
    analysis_timestamp = time.time()
    timestamp_str = datetime.utcfromtimestamp(analysis_timestamp).isoformat() + "Z"
    print(f"[INFO] 분석 시작 시점: {timestamp_str}")

    # 1. 리소스 수집
    cache = ResourceCache()

    # 2. 개별 분석 (10종)
    failures = []
    failures += PodAnalyzer().analyze_from_cache(cache)
    failures += DeploymentAnalyzer().analyze_from_cache(cache)
    failures += NodeAnalyzer().analyze_from_cache(cache)
    failures += ServiceAnalyzer().analyze_from_cache(cache)
    failures += IngressAnalyzer().analyze_from_cache(cache)
    failures += PVCAnalyzer().analyze_from_cache(cache)
    failures += HPAAnalyzer().analyze_from_cache(cache)
    failures += JobAnalyzer().analyze_from_cache(cache)
    failures += CronJobAnalyzer().analyze_from_cache(cache)
    failures += PVAnalyzer().analyze_from_cache(cache)
    print(f"[INFO] 탐지된 장애: {len(failures)}건")

    # 3. 의존성 그래프 + RCA
    graph = DependencyGraph(cache)
    root_causes = graph.find_root_causes()
    print(f"[INFO] 근본원인: {len(root_causes)}건")

    # 4. 이벤트 분석 (시점 정렬: 최근 15분)
    event_chains = EventAnalyzer().analyze(since_minutes=15)
    print(f"[INFO] 이벤트 체인: {len(event_chains)}건")

    # 5. 컨텍스트 구성
    error_summary = graph.get_error_summary_for_llm()
    rca_summary = _build_rca_context(root_causes)

    # 6. 메트릭 (시점 정렬)
    try:
        metrics_analyzer = MetricsAnalyzer()
        metrics_context = metrics_analyzer.build_metrics_context_for_llm(
            reference_time=analysis_timestamp
        )
    except Exception as e:
        print(f"[WARN] 메트릭 수집 실패, 메트릭 없이 진행: {e}")
        metrics_context = "\n--- [Prometheus 메트릭] ---\n  메트릭 수집 실패\n"

    # 7. full_context 구성 (시점 명시)
    timestamp_header = f"[분석 기준 시점: {timestamp_str}]\n\n"
    full_context = timestamp_header + error_summary + rca_summary + metrics_context

    # 8. RAG 검색 (에러 키워드 기반 쿼리)
    rag_query = _build_rag_query(root_causes)
    retriever_inst = Retriever()
    rag_docs = retriever_inst.search_texts_only(rag_query)

    # 9. LLM 분석
    llm_inst = LLMClient()
    final_report = llm_inst.analyze(full_context, rag_docs)

    print("\n" + "=" * 60)
    print("📋 KubeIn 분석 리포트")
    print("=" * 60)
    print(final_report)
    print("=" * 60)

    return {
        "failures": [str(f) for f in failures],
        "root_causes": [rc.build_summary() for rc in root_causes],
        "event_chains": [ec.build_summary() for ec in event_chains],
        "report": final_report,
    }


if __name__ == "__main__":
    run_analysis()
