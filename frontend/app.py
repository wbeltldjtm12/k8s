import streamlit as st
import requests
import os
from collections import defaultdict, deque
from datetime import datetime


st.set_page_config(
    page_title="KubeInsight — K8s 장애 진단",
    page_icon="KubeInsight",
    layout="wide",
)

# ── 전체 CSS 스타일 ──────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #f8fafc; }
    .error-card {
        background: white; border-radius: 4px; padding: 12px 16px;
        margin-bottom: 8px; border-left: 4px solid #e2e8f0;
        cursor: pointer; transition: all 0.2s;
        border: 1px solid #e5e7eb;
    }
    .error-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
    .error-card.p1 { border-left-color: #ef4444; }
    .error-card.p2 { border-left-color: #f97316; }
    .error-card.p3 { border-left-color: #eab308; }
    .error-card.p4 { border-left-color: #3b82f6; }
    .error-card.p5 { border-left-color: #94a3b8; }
    .error-card .kind-badge {
        display: inline-block; font-size: 11px; padding: 2px 8px;
        border-radius: 4px; font-weight: 600; color: white; margin-right: 6px;
    }
    .badge-pod { background: #ef4444; }
    .badge-node { background: #8b5cf6; }
    .badge-deploy { background: #3b82f6; }
    .badge-service { background: #10b981; }
    .badge-pvc { background: #f59e0b; }
    .badge-ingress { background: #6366f1; }
    .badge-cm { background: #64748b; }
    .badge-default { background: #94a3b8; }
    .error-card .name { font-weight: 600; font-size: 14px; color: #1e293b; }
    .error-card .reason { font-size: 12px; color: #64748b; margin-top: 2px; }
    .error-card .ns { font-size: 11px; color: #94a3b8; }
    .rc-box {
        background: #ffffff;
        border: 1px solid #e5e7eb; border-radius: 4px;
        padding: 16px; margin-bottom: 12px;
    }
    .rc-box .rc-title { font-size: 18px; font-weight: 700; color: #dc2626; }
    .rc-box .rc-detail { font-size: 13px; color: #374151; margin-top: 8px; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { border-radius: 4px; padding: 8px 16px; }
    .blast-summary-card {
        background: #ffffff;
        border: 1px solid #e5e7eb; border-radius: 4px;
        padding: 16px; text-align: center;
    }
    .blast-summary-card .blast-number { font-size: 20px; font-weight: 800; color: #dc2626; }
    .blast-summary-card .blast-label { font-size: 12px; color: #64748b; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

# ── 상수 ─────────────────────────────────────────────────────────────
KIND_BADGE_CLASS = {
    "Pod": "badge-pod", "Node": "badge-node",
    "Deployment": "badge-deploy", "Service": "badge-service",
    "PVC": "badge-pvc", "Ingress": "badge-ingress",
    "ConfigMap": "badge-cm", "Secret": "badge-cm",
}

PRIORITY_LABELS = {
    1: ("P1", "Missing 리소스", "p1"),
    2: ("P2", "인프라 장애", "p2"),
    3: ("P3", "스토리지 장애", "p3"),
    4: ("P4", "워크로드 장애", "p4"),
    5: ("P5", "상위 리소스", "p5"),
}

STATUS_COLOR = {"ERROR": "#dc2626", "WARNING": "#f59e0b", "OK": "#22c55e"}

KIND_SHORT = {
    "Deployment": "Deploy", "ReplicaSet": "RS", "StatefulSet": "STS",
    "DaemonSet": "DS", "ConfigMap": "CM", "StorageClass": "SC",
    "Service": "Svc", "Ingress": "Ing", "PersistentVolumeClaim": "PVC",
}

# ── session_state 초기화 ─────────────────────────────────────────────
if "history" not in st.session_state:
    st.session_state.history = []
if "current_idx" not in st.session_state:
    st.session_state.current_idx = None

# ── 사이드바 ────────────────────────────────────────────────────────
st.sidebar.header("시스템 설정 및 상태")
st.sidebar.markdown(f"**백엔드 API:**\n`{BACKEND_URL}`")
st.sidebar.caption("엔진: KubeInsight RCA")

if st.sidebar.button("[시스템 상태 확인]"):
    try:
        resp = requests.get(f"{BACKEND_URL}/api/health", timeout=5)
        if resp.status_code == 200:
            health = resp.json()
            k8s = health.get("kubernetes", {})
            if k8s.get("status") == "connected":
                st.sidebar.success(f"[OK] K8s 연결됨 (노드 {k8s.get('nodes', '?')}개)")
            else:
                st.sidebar.error(f"[ERR] K8s 연결 실패: {k8s.get('message', '')}")
            llm_status = health.get("llm", {})
            if llm_status.get("status") == "connected":
                st.sidebar.success(f"[OK] LLM 연결됨 ({llm_status.get('model', '모델 미지정')})")
            elif llm_status.get("status") == "configured":
                st.sidebar.warning(
                    f"[설정됨] LLM 연결은 미검증 ({llm_status.get('model', '모델 미지정')})"
                )
            else:
                st.sidebar.error(f"[ERR] LLM: {llm_status.get('message', '')}")
            chroma = health.get("chromadb", {})
            if chroma.get("status") == "ready":
                st.sidebar.success(f"[OK] ChromaDB ({chroma.get('chunks', 0)}개 청크)")
            else:
                st.sidebar.warning("[WARN] ChromaDB 비어있음")
        else:
            st.sidebar.error(f"[ERR] 헬스체크 실패 (HTTP {resp.status_code})")
    except (requests.exceptions.RequestException, ValueError) as exc:
        st.sidebar.error(f"[ERR] 헬스체크 요청 실패: {exc}")

st.sidebar.markdown("---")
st.sidebar.markdown("### [분석 히스토리]")
history = st.session_state.history
if history:
    labels = [f"[{i+1}] {h['timestamp']}" for i, h in enumerate(reversed(history))]
    selected = st.sidebar.radio(
        "과거 분석 결과 선택:",
        options=range(len(labels)),
        format_func=lambda i: labels[i],
        index=0,
        key="history_radio",
    )
    st.session_state.current_idx = len(history) - 1 - selected
    if st.sidebar.button("[히스토리 전체 삭제]"):
        st.session_state.history = []
        st.session_state.current_idx = None
        st.rerun()
else:
    st.sidebar.info("아직 분석 결과가 없습니다.")

st.sidebar.markdown("---")

# ── 메인 화면 ───────────────────────────────────────────────────────
st.title("KubeInsight — K8s 장애 진단 대시보드")
st.markdown(
    "**자체 Analyzer 엔진 + RAG + OpenAI 호환 LLM** 기반으로 "
    "Kubernetes 클러스터의 장애를 자동 진단하고 해결 방법을 제시합니다."
)

if st.button("클러스터 전체 분석", use_container_width=True, key="run_all"):
    with st.spinner("클러스터 스캔 중... (3~5초)"):
        try:
            resp = requests.get(f"{BACKEND_URL}/api/causality_fast", timeout=60)
        except requests.exceptions.ConnectionError:
            st.error("[ERR] 백엔드 서버에 연결할 수 없습니다.")
            st.stop()
        except requests.exceptions.Timeout:
            st.error("[ERR] 스캔 시간 초과 (60초).")
            st.stop()
        except Exception as e:
            st.error(f"[ERR] 오류: {e}")
            st.stop()

    if resp.status_code != 200:
        st.error(f"[ERR] 백엔드 오류 (HTTP {resp.status_code}): {resp.text}")
        st.stop()

    payload = resp.json().get("data", {})
    entry = {
        "timestamp": datetime.now().strftime("%m/%d %H:%M:%S"),
        "payload": payload,
        "ai_analysis": "",
    }
    st.session_state.history.append(entry)
    st.session_state.current_idx = len(st.session_state.history) - 1
    st.rerun()


# ══════════════════════════════════════════════════════════════════════
# 트리 렌더링 핵심 함수
# ══════════════════════════════════════════════════════════════════════

def _short_name(nid: str) -> str:
    name = nid.split("/")[-1]
    return (name[:23] + "..") if len(name) > 25 else name


def _colorize_tree(text: str) -> str:
    """[ERR]/[WAR]/[OK]/[MIS] 태그에 html 색상을 입혀 st.markdown으로 시각화.
    Streamlit markdown 렌더러가 <pre> 안의 \\n을 제거하므로 \\n을 <br>로 변환한다.
    """
    # HTML escape (< > & 만 — \n 은 아직 건드리지 않는다)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 상태 태그에 색상 적용
    replacements = {
        "[ERR]": '<span style="color:#dc2626;font-weight:700">[ERR]</span>',
        "[WAR]": '<span style="color:#f59e0b;font-weight:700">[WAR]</span>',
        "[OK]":  '<span style="color:#22c55e;font-weight:700">[OK]</span>',
        "[MIS]": '<span style="color:#6b7280;font-weight:700">[MIS]</span>',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Streamlit markdown이 <pre> 내부 \n을 제거하므로 명시적 <br>로 변환
    text = text.replace("\n", "<br>")
    return (
        '<div style="background:#f8fafc;padding:12px 16px;border-radius:4px;'
        'font-size:13px;line-height:1.7;overflow-x:auto;border:1px solid #e5e7eb;'
        "font-family:'JetBrains Mono','Fira Code','Cascadia Code','Consolas',ui-monospace,monospace;\">"
        + text + "</div>"
    )


def _node_label(nid: str, node_lookup: dict) -> str:
    n = node_lookup.get(nid)
    short = _short_name(nid)
    if n:
        icon = {"ERROR": "[ERR]", "WARNING": "[WAR]", "OK": "[OK]"}.get(n["status"], "[MIS]")
        kind = KIND_SHORT.get(n["kind"], n["kind"])
        ns = n.get("namespace") or "cluster"
        reason = n.get("error_reason") or "정상"
        return f"{icon} {kind}/{ns}/{short} ({reason})"
    else:
        parts = nid.split("/")
        if len(parts) == 3:
            kind, ns, _ = parts
        elif len(parts) == 2:
            kind, _ = parts
            ns = "cluster"
        else:
            kind = parts[0] if parts else "?"
            ns = "?"
        kind = KIND_SHORT.get(kind, kind)
        return f"[MIS] {kind}/{ns}/{short} (Missing)"


def _build_tree(root_id: str, gd_edges: list, valid_ids: set) -> dict:
    """root_id를 루트로 BFS 트리 구성. 방향 무관하게 연결."""
    forward = defaultdict(set)
    reverse = defaultdict(set)
    for e in gd_edges:
        s, t = e["source"], e["target"]
        if s in valid_ids and t in valid_ids:
            forward[s].add(t)
            reverse[t].add(s)

    children = defaultdict(list)
    visited = {root_id}
    queue = deque([root_id])

    while queue:
        current = queue.popleft()
        for nxt in forward.get(current, set()):
            if nxt not in visited and nxt in valid_ids:
                visited.add(nxt)
                children[current].append(nxt)
                queue.append(nxt)
        for nxt in reverse.get(current, set()):
            if nxt not in visited and nxt in valid_ids:
                visited.add(nxt)
                children[current].append(nxt)
                queue.append(nxt)

    # valid_ids 중 트리에 포함 안 된 노드는 루트 직속으로
    for nid in valid_ids:
        if nid not in visited and nid != root_id:
            children[root_id].append(nid)

    return children


def _render_tree_lines(node_id: str, children: dict, node_lookup: dict,
                       prefix: str = "", is_last: bool = True, is_root: bool = True) -> list:
    lines = []
    if is_root:
        lines.append(_node_label(node_id, node_lookup))
    else:
        connector = "└─ " if is_last else "├─ "
        lines.append(prefix + connector + _node_label(node_id, node_lookup))

    child_list = children.get(node_id, [])
    for i, child_id in enumerate(child_list):
        is_child_last = (i == len(child_list) - 1)
        if is_root:
            child_prefix = "  "
        else:
            child_prefix = prefix + ("    " if is_last else "│   ")
        lines.extend(_render_tree_lines(
            child_id, children, node_lookup,
            prefix=child_prefix, is_last=is_child_last, is_root=False
        ))
    return lines


def _render_chain_core(chain: dict, gd_nodes: list, gd_edges: list,
                       show_blast_summary: bool = True,
                       show_score_header: bool = True) -> str:
    """체인 하나를 트리 텍스트로 변환한다. HTML이 아닌 순수 텍스트 반환."""
    node_lookup = {n["id"]: n for n in gd_nodes}

    rc_kind = chain.get("root_cause_kind", "")
    rc_ns = chain.get("root_cause_namespace", "") or "cluster"
    rc_name = chain.get("root_cause", "")
    rc_id = f"{rc_kind}/{rc_ns}/{rc_name}"
    rc_reason = chain.get("root_cause_reason", "")
    score = chain.get("score", "?")
    blast = chain.get("blast_radius", "?")
    depth = chain.get("depth", "?")

    # 유효 노드 id 수집
    affected_ids = chain.get("affected_node_ids") or []
    if affected_ids:
        valid_ids = set(affected_ids)
        valid_ids.add(rc_id)
    else:
        summary = chain.get("chain_summary", "")
        valid_ids = set()
        for part in summary.split("→"):
            res_path = part.strip().split("(")[0].strip()
            if "파급 범위:" in res_path or "스코어:" in res_path:
                continue
            if "/" in res_path:
                valid_ids.add(res_path)
        if not valid_ids:
            valid_ids = {rc_id}
    valid_ids.add(rc_id)

    # 트리 구성
    children = _build_tree(rc_id, gd_edges, valid_ids)

    # 헤더
    rc_kind_short = KIND_SHORT.get(rc_kind, rc_kind)
    rc_short_name = _short_name(rc_id)
    rc_status = node_lookup.get(rc_id, {}).get("status", "ERROR")
    rc_icon = {"ERROR": "[ERR]", "WARNING": "[WAR]", "OK": "[OK]"}.get(rc_status, "[MIS]")

    if show_score_header:
        header = f"{rc_icon} Root Cause: {rc_kind_short}/{rc_ns}/{rc_short_name} ({rc_reason})"
        sub_header = f"│  Score: {score} | 파급 범위: {blast}개 리소스 | 깊이: {depth}"
        tree_lines = [header, sub_header, "│"]
    else:
        header = f"{rc_icon} Root Cause: {rc_kind_short}/{rc_ns}/{rc_short_name} ({rc_reason}) — Score: {score}, Blast: {blast}"
        tree_lines = [header]

    # 트리 본문
    child_list = children.get(rc_id, [])
    for i, child_id in enumerate(child_list):
        is_last = (i == len(child_list) - 1)
        tree_lines.extend(_render_tree_lines(
            child_id, children, node_lookup,
            prefix="  ", is_last=is_last, is_root=False
        ))

    # 트리에 자식이 없으면 chain_summary 폴백
    if len(child_list) == 0:
        summary = chain.get("chain_summary", "")
        if summary:
            tree_lines.append("")
            tree_lines.append("  [전파 경로]")
            for part in summary.split("→"):
                p = part.strip()
                if p:
                    tree_lines.append(f"    → {p}")

    # 파급 요약 (선택적 표시)
    if show_blast_summary:
        error_cnt = 0
        warn_cnt = 0
        ok_cnt = 0
        for nid in valid_ids:
            if nid == rc_id:
                continue
            n = node_lookup.get(nid)
            if n:
                if n["status"] == "ERROR":
                    error_cnt += 1
                elif n["status"] == "WARNING":
                    warn_cnt += 1
                else:
                    ok_cnt += 1
            else:
                error_cnt += 1  # Missing 노드

        tree_lines.append("")
        tree_lines.append(
            f"└─ [파급 요약] [ERR]: {error_cnt}개 | "
            f"[WAR]: {warn_cnt}개 | [OK]: {ok_cnt}개"
        )

    return "\n".join(tree_lines)


def render_chain_tree_with_blast(chain: dict, gd_nodes: list, gd_edges: list):
    text = _render_chain_core(chain, gd_nodes, gd_edges, show_blast_summary=True, show_score_header=True)
    if not text.strip():
        st.info("표시할 데이터가 없습니다.")
    else:
        st.markdown(_colorize_tree(text), unsafe_allow_html=True)


def render_chain_tree(chain: dict, gd_nodes: list, gd_edges: list):
    text = _render_chain_core(chain, gd_nodes, gd_edges, show_blast_summary=False, show_score_header=False)
    if not text.strip():
        st.info("표시할 데이터가 없습니다.")
    else:
        st.markdown(_colorize_tree(text), unsafe_allow_html=True)



# ── 심각도별 색상 ────────────────────────────────────────────────────
PRIO_BG = {
    1: ("#fef2f2", "#dc2626"),
    2: ("#fff7ed", "#ea580c"),
    3: ("#fefce8", "#ca8a04"),
    4: ("#eff6ff", "#2563eb"),
    5: ("#f8fafc", "#64748b"),
}


def render_all_chains_tree(chains: list, gd_nodes: list, gd_edges: list, entry_idx: int = 0):
    """전체 체인을 접이식 expander로 렌더링 — 파급 정보 통합 + 개별 체인 분석"""
    if not chains:
        st.info("표시할 체인이 없습니다.")
        return

    for i, chain in enumerate(chains):
        prio = chain.get("root_cause_priority", 5)
        kind = chain.get("root_cause_kind", "")
        name = chain.get("root_cause", "")
        reason = chain.get("root_cause_reason", "")
        score = chain.get("score", "?")
        blast = chain.get("blast_radius", "?")

        bg_color, border_color = PRIO_BG.get(prio, ("#f8fafc", "#64748b"))
        p_label = PRIORITY_LABELS.get(prio, ("기타", "기타", ""))[1]
        kind_short = KIND_SHORT.get(kind, kind)

        summary_line = (
            f"체인 #{i+1}  {p_label}  |  "
            f"{kind_short}: {name[:25]}  |  {reason}  |  "
            f"Score: {score}  |  Blast: {blast}"
        )

        st.markdown(
            f'<div style="background:{bg_color}; border-left:4px solid {border_color}; '
            f'border-radius:6px; padding:8px 12px; margin-bottom:2px;">'
            f'<span style="font-size:13px; font-weight:600; color:{border_color};">'
            f'{summary_line}</span></div>',
            unsafe_allow_html=True,
        )

        with st.expander("상세 트리 + 파급 범위 보기", expanded=False):
            render_chain_tree_with_blast(chain, gd_nodes, gd_edges)

            st.markdown("---")

            # ── 개별 체인 분석 ──────────────────────────────────────────
            ai_key = f"chain_ai_{entry_idx}_{i}"
            cached = st.session_state.get(ai_key, "")

            if cached:
                with st.expander("분석 결과 보기", expanded=True):
                    st.text(cached)
                if st.button("다시 분석", key=f"reanalyze_{entry_idx}_{i}", type="secondary"):
                    del st.session_state[ai_key]
                    st.rerun()
            else:
                placeholder = st.empty()
                if placeholder.button(
                    "이 체인 분석",
                    key=f"analyze_{entry_idx}_{i}",
                    type="primary",
                    use_container_width=True,
                ):
                    placeholder.empty()
                    chain_summary = chain.get("chain_summary", "")
                    prompt = (
                        f"Root Cause: {kind}/{name} (우선순위: {p_label}, 원인: {reason})\n"
                        f"전파 경로: {chain_summary}"
                    )
                    with st.spinner("분석 중... (약 30초 소요)"):
                        try:
                            resp = requests.post(
                                f"{BACKEND_URL}/api/ai_analysis",
                                json={"summaries": prompt},
                                timeout=120,
                            )
                            if resp.status_code == 200:
                                result = resp.json().get("analysis", "").strip()
                                st.session_state[ai_key] = result
                                with st.expander("분석 결과 보기", expanded=True):
                                    st.text(result)
                            else:
                                st.error(f"분석 오류 — HTTP {resp.status_code}: {resp.text[:200]}")
                        except requests.exceptions.Timeout:
                            st.error("타임아웃: 백엔드 응답이 120초를 초과했습니다. 잠시 후 다시 시도하세요.")
                        except requests.exceptions.ConnectionError as ex:
                            st.error(f"연결 오류: 백엔드({BACKEND_URL})에 연결할 수 없습니다. ({ex})")
                        except Exception as ex:
                            st.error(f"오류 ({type(ex).__name__}): {ex}")




# ══════════════════════════════════════════════════════════════════════
# 결과 렌더링
# ══════════════════════════════════════════════════════════════════════

def render_result(entry: dict, entry_idx: int):
    payload = entry["payload"]
    chains = payload.get("chains", [])
    gd = payload.get("graph_data", {})
    gd_nodes = gd.get("nodes", [])
    gd_edges = gd.get("edges", [])
    ai_text = entry.get("ai_analysis", "")

    failures = [n for n in gd_nodes if n["status"] in ("ERROR", "WARNING")]
    node_lookup = {n["id"]: n for n in gd_nodes}

    if not chains:
        st.info("클러스터에 감지된 장애가 없습니다.")
        return

    error_count = len([n for n in gd_nodes if n["status"] == "ERROR"])
    warn_count = len([n for n in gd_nodes if n["status"] == "WARNING"])
    ok_count = len([n for n in gd_nodes if n["status"] == "OK"])

    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    col_s1.metric("인과 체인", f"{len(chains)}개")
    col_s2.metric("에러 노드", f"{error_count}개")
    col_s3.metric("경고 노드", f"{warn_count}개")
    col_s4.metric("정상 노드", f"{ok_count}개")

    sel_key = f"sel_chain_{entry_idx}"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = -1

    tab_overview, tab_chains = st.tabs([
        "전체 인과 체인 · 파급 범위", "개별 체인 분석"
    ])

    # ── 탭 1: 전체 인과 체인 + 파급 범위 (통합) ─────────────────
    with tab_overview:
        col_g, col_p = st.columns([7, 3])

        with col_g:
            st.markdown("#### 전체 인과 체인 · 파급 범위")
            st.caption(
                f"총 {len(chains)}개 체인 · {error_count} ERROR · {warn_count} WARNING · "
                "각 체인을 펼쳐 트리와 파급 범위를 한눈에 확인하세요"
            )
            render_all_chains_tree(chains, gd_nodes, gd_edges, entry_idx=entry_idx)

        with col_p:
            st.markdown("#### 감지된 장애")
            show_sys = st.checkbox(
                "시스템 컴포넌트 보기 (kube-system)",
                value=False,
                key=f"show_sys_{entry_idx}",
            )
            visible_failures = [
                n for n in failures
                if show_sys or n.get("namespace") not in ("kube-system", "kube-public", "kube-node-lease")
            ]
            if not visible_failures:
                st.info("사용자 네임스페이스 장애 없음 (시스템 컴포넌트 보기를 활성화하세요)")
            for n in visible_failures:
                kind = n["kind"]
                name = n["id"].split("/")[-1]
                reason = n.get("error_reason", "")
                ns = n.get("namespace") or "cluster"
                badge_cls = KIND_BADGE_CLASS.get(kind, "badge-default")
                status_color = STATUS_COLOR.get(n["status"], "#64748b")

                st.markdown(f"""
                <div class="error-card {('p1' if 'Missing' in reason else 'p4')}" style="margin-bottom:12px;">
                    <span class="kind-badge {badge_cls}">{kind}</span>
                    <span class="ns">{ns}</span>
                    <div class="name"><span style="color:{status_color}">●</span> {name}</div>
                    <div class="reason">{reason}</div>
                </div>
                """, unsafe_allow_html=True)

            # 클러스터 전체 파급 요약 카드
            st.markdown("---")
            st.markdown("#### 클러스터 파급 요약")
            total_blast = sum(c.get("blast_radius", 0) for c in chains)
            max_score = max((c.get("score", 0) for c in chains), default=0)
            st.markdown(
                f'<div class="blast-summary-card">'
                f'<div class="blast-number">{total_blast}</div>'
                f'<div class="blast-label">총 파급 리소스</div>'
                f'</div>', unsafe_allow_html=True
            )
            st.markdown(
                f'<div class="blast-summary-card" style="margin-top:8px;">'
                f'<div class="blast-number">{max_score}</div>'
                f'<div class="blast-label">최고 RCA 스코어</div>'
                f'</div>', unsafe_allow_html=True
            )

    # ── 탭 2: 개별 체인 분석 ────────────────────────────────────────
    with tab_chains:
        col_graph, col_panel = st.columns([6, 4])

        with col_panel:
            st.markdown(f"#### 인과 체인 ({len(chains)}개)")
            st.caption("체인을 선택하면 왼쪽에 상세 트리와 파급 범위가 표시됩니다.")

            for idx, chain in enumerate(chains):
                kind = chain.get("root_cause_kind", "")
                name = chain.get("root_cause", "")
                reason = chain.get("root_cause_reason", "")
                is_sel = (st.session_state[sel_key] == idx)

                if st.button(
                    f"{'▶ ' if is_sel else ''}{kind}: {name[:22]} — {reason}",
                    key=f"chain_btn_{entry_idx}_{idx}",
                    use_container_width=True,
                ):
                    st.session_state[sel_key] = idx
                    st.rerun()

            st.markdown("---")

            sel = st.session_state[sel_key]
            if 0 <= sel < len(chains):
                top = chains[sel]
                prio = top.get("root_cause_priority", 5)
                kind = top.get("root_cause_kind", "")
                name = top.get("root_cause", "")
                ns = top.get("root_cause_namespace", "") or "cluster"
                reason = top.get("root_cause_reason", "")
                msg = top.get("root_cause_message", "")
                p_label = PRIORITY_LABELS.get(prio, ("기타", "p5"))[0]

                st.markdown(f"""
                <div class="rc-box">
                    <div class="rc-title">Root Cause — {p_label}</div>
                    <div class="rc-detail">
                        <b>{kind}</b> <code>{ns}/{name}</code><br/>
                        원인: <code>{reason}</code>
                        {"<br/>" + msg if msg else ""}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                # 파급 범위 요약 카드 (개별 체인)
                bc1, bc2 = st.columns(2)
                bc1.markdown(
                    f'<div class="blast-summary-card">'
                    f'<div class="blast-number">{top.get("blast_radius", "?")}</div>'
                    f'<div class="blast-label">Blast Radius</div>'
                    f'</div>', unsafe_allow_html=True
                )
                bc2.markdown(
                    f'<div class="blast-summary-card">'
                    f'<div class="blast-number">{top.get("score", "?")}</div>'
                    f'<div class="blast-label">RCA Score</div>'
                    f'</div>', unsafe_allow_html=True
                )

                with st.expander("전파 경로 (원본)", expanded=False):
                    chain_summary = top.get("chain_summary", "")
                    formatted = chain_summary.replace(" → ", "\n  → ")
                    st.code(formatted, language="text")

                with st.expander("권장 조치", expanded=False):
                    if ai_text:
                        sections = []
                        current_title = "분석 결과"
                        current_lines = []
                        for line in ai_text.split("\n"):
                            stripped = line.strip()
                            if stripped.startswith("## ") or stripped.startswith("🚨") or stripped.startswith("�") or stripped.startswith("�🔍") or stripped.startswith("💡") or stripped == "---":
                                if stripped == "---":
                                    continue
                                if current_lines:
                                    sections.append((current_title, "\n".join(current_lines)))
                                    current_lines = []
                                current_title = stripped.lstrip("#").strip()
                            else:
                                current_lines.append(line)
                        if current_lines:
                            sections.append((current_title, "\n".join(current_lines)))

                        if len(sections) <= 1:
                            with st.expander("분석 결과 보기", expanded=True):
                                st.markdown(ai_text)
                        else:
                            for sec_i, (title, body) in enumerate(sections):
                                with st.expander(title, expanded=(sec_i == 0)):
                                    st.markdown(body)

                        if "kubectl" in ai_text:
                            cmds = [l.strip() for l in ai_text.split("\n") if "kubectl" in l]
                            if cmds:
                                st.markdown("**명령어:**")
                                st.code("\n".join(cmds), language="bash")
                    else:
                        st.caption("아직 상세 분석을 진행하지 않았습니다.")
                        if st.button("상세 원인 분석 요청",
                                     key=f"ai_btn_{entry_idx}"):
                            summaries = "\n\n".join(
                                c.get("chain_summary", "") for c in chains[:5]
                            )
                            with st.spinner("LLM 분석 중..."):
                                try:
                                    ai_resp = requests.post(
                                        f"{BACKEND_URL}/api/ai_analysis",
                                        json={"summaries": summaries},
                                        timeout=120,
                                    )
                                    if ai_resp.status_code == 200:
                                        raw = ai_resp.json().get("analysis", "")
                                        st.session_state.history[entry_idx]["ai_analysis"] = raw.strip()
                                        st.rerun()
                                    else:
                                        st.error(f"[ERR] 분석 오류 (HTTP {ai_resp.status_code})")
                                except Exception as ex:
                                    st.error(f"[ERR] 오류: {ex}")
            else:
                st.info("-> 체인을 선택하세요.")

        with col_graph:
            sel = st.session_state[sel_key]
            if 0 <= sel < len(chains):
                sel_chain = chains[sel]
                sel_name = sel_chain.get("root_cause", "")
                sel_reason = sel_chain.get("root_cause_reason", "")
                prio = sel_chain.get("root_cause_priority", 5)

                st.markdown(f"#### 체인 #{sel+1} — {sel_name} ({sel_reason})")
                render_chain_tree_with_blast(sel_chain, gd_nodes, gd_edges)

                # 레이어별 상세 expander
                st.markdown("---")
                st.markdown("##### 레이어별 영향 분석")

                rc_kind = sel_chain.get("root_cause_kind", "")
                rc_ns = sel_chain.get("root_cause_namespace", "") or "cluster"
                rc_name_b = sel_chain.get("root_cause", "")
                rc_id = f"{rc_kind}/{rc_ns}/{rc_name_b}"
                affected_ids = sel_chain.get("affected_node_ids") or []

                id_to_layer = {}
                if affected_ids:
                    id_to_layer[rc_id] = 0
                    for nid in affected_ids:
                        if nid != rc_id:
                            id_to_layer[nid] = id_to_layer.get(nid, 1)
                    ordered_ids = [rc_id] + [nid for nid in affected_ids if nid != rc_id]
                else:
                    summary = sel_chain.get("chain_summary", "")
                    ordered_ids = []
                    layer = 0
                    for part in summary.split("→"):
                        res_path = part.strip().split("(")[0].strip()
                        if "파급 범위:" in res_path or "스코어:" in res_path:
                            continue
                        if "/" in res_path and res_path not in id_to_layer:
                            id_to_layer[res_path] = layer
                            ordered_ids.append(res_path)
                            layer += 1
                    if not ordered_ids:
                        ordered_ids = [rc_id]
                        id_to_layer = {rc_id: 0}

                if rc_id not in id_to_layer:
                    id_to_layer[rc_id] = 0
                    ordered_ids.insert(0, rc_id)

                layer_map = defaultdict(list)
                for nid in ordered_ids:
                    layer_map[id_to_layer.get(nid, 3)].append((nid, node_lookup.get(nid)))

                layer_labels = {
                    0: "Layer 0 — Root Cause", 1: "Layer 1 — 직접 영향",
                    2: "Layer 2 — 간접 영향", 3: "Layer 3+ — 연쇄 영향"
                }

                for lyr in sorted(layer_map.keys()):
                    items = layer_map[lyr]
                    lbl = layer_labels.get(lyr, f"Layer {lyr} — 연쇄 영향")
                    with st.expander(f"▶ {lbl} ({len(items)}개)", expanded=(lyr <= 1)):
                        for nid, n in items:
                            if n:
                                icon = {"ERROR": "[ERR]", "WARNING": "[WAR]", "OK": "[OK]"}.get(n["status"], "[MIS]")
                                rsn = n.get("error_reason") or "정상"
                                st.markdown(f"`{nid}` {icon} **{rsn}**")
                            else:
                                st.markdown(f"`{nid}` [MIS] Missing")
            else:
                st.markdown("#### 체인을 선택하면 트리가 표시됩니다")
                st.caption("오른쪽 패널에서 분석할 체인을 클릭하세요.")
                if chains:
                    render_chain_tree(chains[0], gd_nodes, gd_edges)


# ── 현재 히스토리 항목 렌더링 ────────────────────────────────────────
idx = st.session_state.current_idx
if idx is not None and 0 <= idx < len(st.session_state.history):
    entry = st.session_state.history[idx]
    total = len(st.session_state.history)
    st.caption(
        f"[분석 시각] **{entry['timestamp']}** "
        f"(총 {total}개 히스토리 중 {total - idx}번째)"
    )
    render_result(entry, idx)

# ── 메인 ────────────────────────────────────────────────────────────
st.markdown("## KubeInsight")
st.caption(
    "Kubernetes 클러스터 장애 자동 진단 및 근본 원인 분석 | "
    "DependencyGraph RCA + ChromaDB RAG + OpenAI 호환 LLM"
)

# ── 푸터 ─────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Powered by KubeInsight"
)
