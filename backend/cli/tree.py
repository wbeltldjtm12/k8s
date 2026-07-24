#!/usr/bin/env python3
"""
cli/tree.py
====================
KubeIn 클러스터 리소스 ASCII 트리 출력기 (경량 데이터 모델 기반)
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infra.cache import ResourceCache
from infra.models import MiniPod, MiniContainer, MiniReplicaSet


# ── ANSI 색상 ──────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"
    WHITE  = "\033[97m"

NO_COLOR = False

def c(color: str, text: str) -> str:
    if NO_COLOR:
        return text
    return f"{color}{text}{C.RESET}"


# ── 트리 그리기 헬퍼 ────────────────────────────────────────────────────────
PIPE   = "│   "
TEE    = "├── "
LAST   = "└── "
BLANK  = "    "

# ── 상태 → 아이콘 ───────────────────────────────────────────────────────────
def pod_status_icon(pod: MiniPod) -> str:
    phase = pod.phase or "Unknown"
    ready = (pod.status == "OK")
    restarts = sum(c.restart_count for c in pod.containers)

    if phase == "Running" and ready:
        icon = c(C.GREEN, "[✓]")
    elif phase in ("Pending", "ContainerCreating"):
        icon = c(C.YELLOW, "[~]")
    elif phase in ("Failed", "CrashLoopBackOff", "Error") or not ready:
        icon = c(C.RED, "[✗]")
    else:
        icon = c(C.YELLOW, "[?]")

    restart_str = f" {c(C.YELLOW, f'restarts={restarts}')}" if restarts > 0 else ""
    return f"{icon} {phase}{restart_str}"

def deploy_icon(deploy) -> str:
    avail = deploy.available
    desired = deploy.desired
    if avail >= desired:
        return c(C.GREEN, "[✓]")
    elif avail > 0:
        return c(C.YELLOW, "[~]")
    return c(C.RED, "[✗]")

def node_icon(node) -> str:
    return c(C.GREEN, "[Ready]") if node.ready else c(C.RED, "[NotReady]")

def pvc_icon(pvc) -> str:
    phase = pvc.phase
    if phase == "Bound":
        return c(C.GREEN, "[Bound]")
    elif phase == "Pending":
        return c(C.YELLOW, "[Pending]")
    return c(C.RED, f"[{phase}]")


# ── 빌드 헬퍼: PVC/Endpoint 연결 ───────────────────────────────────────────
def pod_pvcs(pod: MiniPod, cache: ResourceCache):
    """Pod가 마운트하는 PVC 목록"""
    claims = pod.pvc_names
    ns = pod.namespace
    result = []
    for pvc in cache.pvcs:
        if pvc.namespace == ns and pvc.name in claims:
            result.append(pvc)
    return result

def svc_endpoints(svc, cache: ResourceCache):
    if not svc.endpoint_addrs:
        return "no endpoints"
    addrs = svc.endpoint_addrs
    return ", ".join(addrs[:3]) + ("..." if len(addrs) > 3 else "")


# ── 렌더러 ─────────────────────────────────────────────────────────────────
def render_pod(pod: MiniPod, prefix, child_prefix, cache: ResourceCache):
    name = pod.name
    node_name = pod.node_name or "unscheduled"
    icon = pod_status_icon(pod)
    yield f"{prefix}{c(C.CYAN, 'Pod')}: {c(C.WHITE, name)} {icon}  {c(C.GRAY, f'@ {node_name}')}"

    containers = pod.containers
    pvcs = pod_pvcs(pod, cache)
    child_items = containers + pvcs

    for i, item in enumerate(child_items):
        is_last = (i == len(child_items) - 1)
        conn = LAST if is_last else TEE
        if isinstance(item, MiniContainer):
            state_str = ""
            if item.state == "running":
                state_str = c(C.GREEN, "running")
            elif item.state == "waiting":
                state_str = c(C.YELLOW, f"waiting:{item.reason}")
            elif item.state == "terminated":
                state_str = c(C.RED, f"terminated:{item.reason}")
            img = item.image.split("/")[-1][:40]
            yield f"{child_prefix}{conn}{c(C.GRAY, 'ctr')}: {item.name}  {c(C.GRAY, f'img={img}')}  {state_str}"
        else:  # MiniPVC
            yield f"{child_prefix}{conn}{c(C.GRAY, 'pvc')}: {item.name} {pvc_icon(item)}"


def render_rs(rs: MiniReplicaSet, prefix, child_prefix, cache: ResourceCache, pods_by_rs):
    avail = rs.ready
    desired = rs.desired
    color = C.GREEN if avail >= desired else (C.YELLOW if avail > 0 else C.RED)
    yield f"{prefix}{c(C.GRAY, 'RS')}: {rs.name}  {c(color, f'{avail}/{desired}')}"

    rs_pods = pods_by_rs.get((rs.namespace, rs.name), [])
    for i, pod in enumerate(rs_pods):
        is_last = (i == len(rs_pods) - 1)
        conn = LAST if is_last else TEE
        child2 = child_prefix + (BLANK if is_last else PIPE)
        yield from render_pod(pod, child_prefix + conn, child2, cache)


def render_deploy(deploy, prefix, child_prefix, cache, rs_by_deploy, pods_by_rs):
    avail = deploy.ready
    desired = deploy.desired
    icon = deploy_icon(deploy)
    yield f"{prefix}{c(C.BLUE, 'Deploy')}: {c(C.WHITE, deploy.name)}  {icon}  {c(C.GRAY, f'{avail}/{desired}')}"

    rs_list = rs_by_deploy.get((deploy.namespace, deploy.name), [])
    active_rs = [rs for rs in rs_list if rs.desired > 0]
    show_rs = active_rs or rs_list[:1]

    for i, rs in enumerate(show_rs):
        is_last = (i == len(show_rs) - 1)
        conn = LAST if is_last else TEE
        child2 = child_prefix + (BLANK if is_last else PIPE)
        yield from render_rs(rs, child_prefix + conn, child2, cache, pods_by_rs)


def render_sts(sts, prefix, child_prefix, cache, pods_by_sts):
    avail = sts.ready
    desired = sts.desired
    color = C.GREEN if avail >= desired else (C.YELLOW if avail > 0 else C.RED)
    yield f"{prefix}{c(C.BLUE, 'STS')}: {c(C.WHITE, sts.name)}  {c(color, f'{avail}/{desired}')}"
    sts_pods = pods_by_sts.get((sts.namespace, sts.name), [])
    for i, pod in enumerate(sts_pods):
        is_last = (i == len(sts_pods) - 1)
        conn = LAST if is_last else TEE
        child2 = child_prefix + (BLANK if is_last else PIPE)
        yield from render_pod(pod, child_prefix + conn, child2, cache)


def render_ds(ds, prefix, child_prefix, cache, pods_by_ds):
    avail = ds.ready
    desired = ds.desired
    color = C.GREEN if avail >= desired else C.YELLOW
    yield f"{prefix}{c(C.BLUE, 'DS')}: {c(C.WHITE, ds.name)}  {c(color, f'{avail}/{desired}')}"
    ds_pods = pods_by_ds.get((ds.namespace, ds.name), [])
    for i, pod in enumerate(ds_pods):
        is_last = (i == len(ds_pods) - 1)
        conn = LAST if is_last else TEE
        child2 = child_prefix + (BLANK if is_last else PIPE)
        yield from render_pod(pod, child_prefix + conn, child2, cache)


# ── 네임스페이스 뷰 ─────────────────────────────────────────────────────────
def build_indexes(cache: ResourceCache):
    """Pod/RS를 (namespace, owner_name) 키로 그룹화"""
    rs_by_deploy = {}
    for rs in cache.replica_sets:
        if rs.owner_name:
            key = (rs.namespace, rs.owner_name)
            rs_by_deploy.setdefault(key, []).append(rs)

    pods_by_rs  = {}
    pods_by_sts = {}
    pods_by_ds  = {}
    orphan_pods = []

    for pod in cache.pods:
        if pod.owner:
            kind, owner_name = pod.owner
            key = (pod.namespace, owner_name)
            if kind == "ReplicaSet":
                pods_by_rs.setdefault(key, []).append(pod)
            elif kind == "StatefulSet":
                pods_by_sts.setdefault(key, []).append(pod)
            elif kind == "DaemonSet":
                pods_by_ds.setdefault(key, []).append(pod)
            else:
                orphan_pods.append(pod)
        else:
            orphan_pods.append(pod)

    return rs_by_deploy, pods_by_rs, pods_by_sts, pods_by_ds, orphan_pods


def print_namespace_view(cache: ResourceCache, ns_filter: str = None):
    rs_by_deploy, pods_by_rs, pods_by_sts, pods_by_ds, orphan_pods = build_indexes(cache)

    all_ns = sorted(set(
        [p.namespace for p in cache.pods] +
        [d.namespace for d in cache.deployments]
    ))
    if ns_filter:
        all_ns = [ns for ns in all_ns if ns == ns_filter]

    print(c(C.BOLD, "\n🌐 CLUSTER — Namespace View"))
    print(c(C.GRAY, "─" * 70))

    for ns_idx, ns in enumerate(all_ns):
        is_last_ns = (ns_idx == len(all_ns) - 1)
        ns_conn = LAST if is_last_ns else TEE
        ns_pfx  = BLANK if is_last_ns else PIPE

        print(f"{ns_conn}{c(C.BOLD + C.YELLOW, 'NS')}: {c(C.WHITE + C.BOLD, ns)}")

        ns_deploys = [d for d in cache.deployments if d.namespace == ns]
        ns_sts     = [s for s in cache.stateful_sets if s.namespace == ns]
        ns_ds      = [d for d in cache.daemon_sets if d.namespace == ns]
        ns_svcs    = [s for s in cache.services if s.namespace == ns]
        ns_ingress = [i for i in cache.ingresses if i.namespace == ns]
        ns_pvcs    = [p for p in cache.pvcs if p.namespace == ns]
        ns_jobs    = [j for j in cache.jobs if j.namespace == ns]
        ns_cjs     = [cj for cj in cache.cronjobs if cj.namespace == ns]
        ns_cms     = [c_ for c_ in cache.configmaps if c_.namespace == ns
                      and not c_.name.startswith("kube-")]
        ns_secrets = [s for s in cache.secrets if s.namespace == ns
                      and s.secret_type != "kubernetes.io/service-account-token"]
        ns_orphans = [p for p in orphan_pods if p.namespace == ns]

        sections = []
        if ns_deploys: sections.append(("Deployments", ns_deploys))
        if ns_sts:     sections.append(("StatefulSets", ns_sts))
        if ns_ds:      sections.append(("DaemonSets", ns_ds))
        if ns_jobs:    sections.append(("Jobs", ns_jobs))
        if ns_cjs:     sections.append(("CronJobs", ns_cjs))
        if ns_svcs:    sections.append(("Services", ns_svcs))
        if ns_ingress: sections.append(("Ingresses", ns_ingress))
        if ns_pvcs:    sections.append(("PVCs", ns_pvcs))
        if ns_cms:     sections.append(("ConfigMaps", ns_cms))
        if ns_secrets: sections.append(("Secrets", ns_secrets))
        if ns_orphans: sections.append(("OrphanPods", ns_orphans))

        for sec_idx, (sec_name, sec_items) in enumerate(sections):
            is_last_sec = (sec_idx == len(sections) - 1)
            sec_conn = ns_pfx + (LAST if is_last_sec else TEE)
            sec_pfx  = ns_pfx + (BLANK if is_last_sec else PIPE)

            print(f"{sec_conn}{c(C.GRAY, sec_name)} ({len(sec_items)})")

            for item_idx, item in enumerate(sec_items):
                is_last_item = (item_idx == len(sec_items) - 1)
                item_conn = sec_pfx + (LAST if is_last_item else TEE)
                item_pfx  = sec_pfx + (BLANK if is_last_item else PIPE)

                if sec_name == "Deployments":
                    for line in render_deploy(item, item_conn, item_pfx, cache, rs_by_deploy, pods_by_rs):
                        print(line)

                elif sec_name == "StatefulSets":
                    for line in render_sts(item, item_conn, item_pfx, cache, pods_by_sts):
                        print(line)

                elif sec_name == "DaemonSets":
                    for line in render_ds(item, item_conn, item_pfx, cache, pods_by_ds):
                        print(line)

                elif sec_name == "Services":
                    ep_str = svc_endpoints(item, cache)
                    svc_type = item.svc_type or "ClusterIP"
                    cluster_ip = item.cluster_ip or "-"
                    print(f"{item_conn}{c(C.CYAN, 'SVC')}: {c(C.WHITE, item.name)}  "
                          f"{c(C.GRAY, svc_type)}  {c(C.GRAY, f'clusterIP={cluster_ip}')}  "
                          f"→ {c(C.GRAY, f'ep=[{ep_str}]')}")

                elif sec_name == "Ingresses":
                    hosts = ", ".join(item.hosts) or "-"
                    print(f"{item_conn}{c(C.CYAN, 'ING')}: {c(C.WHITE, item.name)}  "
                          f"{c(C.GRAY, f'hosts={hosts}')}")

                elif sec_name == "PVCs":
                    sc = item.storage_class or "-"
                    size = item.size or "-"
                    print(f"{item_conn}{c(C.CYAN, 'PVC')}: {c(C.WHITE, item.name)}  "
                          f"{pvc_icon(item)}  {c(C.GRAY, f'sc={sc} size={size}')}")

                elif sec_name == "ConfigMaps":
                    keys = item.key_count
                    print(f"{item_conn}{c(C.GRAY, 'CM')}: {item.name}  "
                          f"{c(C.GRAY, f'keys={keys}')}")

                elif sec_name == "Secrets":
                    keys = item.key_count
                    stype = item.secret_type or "-"
                    print(f"{item_conn}{c(C.GRAY, 'Secret')}: {item.name}  "
                          f"{c(C.GRAY, f'type={stype} keys={keys}')}")

                elif sec_name == "Jobs":
                    for job in sec_items:
                        is_last_item2 = (sec_items.index(job) == len(sec_items) - 1)
                        conn2 = sec_pfx + (LAST if is_last_item2 else TEE)
                        icon = c(C.GREEN, "[✓]") if job.status == "OK" else c(C.RED, "[✗]")
                        print(f"{conn2}{c(C.CYAN, 'Job')}: {c(C.WHITE, job.name)}  {icon}  "
                              f"{c(C.GRAY, f'succeeded={job.succeeded}/{job.completions} failed={job.failed}')}")

                elif sec_name == "CronJobs":
                    for cj in sec_items:
                        is_last_item2 = (sec_items.index(cj) == len(sec_items) - 1)
                        conn2 = sec_pfx + (LAST if is_last_item2 else TEE)
                        icon = c(C.GREEN, "[✓]") if cj.status == "OK" else c(C.YELLOW, "[~]")
                        last = cj.last_schedule_time or "Never"
                        print(f"{conn2}{c(C.CYAN, 'CronJob')}: {c(C.WHITE, cj.name)}  {icon}  "
                              f"{c(C.GRAY, f'schedule={cj.schedule} last={last}')}")

                elif sec_name == "OrphanPods":
                    for line in render_pod(item, item_conn, item_pfx, cache):
                        print(line)


# ── 노드 뷰 ────────────────────────────────────────────────────────────────
def print_node_view(cache: ResourceCache):
    pods_by_node = {}
    for pod in cache.pods:
        node_name = pod.node_name or "unscheduled"
        pods_by_node.setdefault(node_name, []).append(pod)

    print(c(C.BOLD, "\n🖥️  CLUSTER — Node View"))
    print(c(C.GRAY, "─" * 70))

    for node_idx, node in enumerate(cache.nodes):
        name = node.name
        is_last_node = (node_idx == len(cache.nodes) - 1)
        node_conn = LAST if is_last_node else TEE
        node_pfx  = BLANK if is_last_node else PIPE

        print(f"{node_conn}{c(C.BOLD + C.YELLOW, 'Node')}: {c(C.WHITE + C.BOLD, name)}  "
              f"{node_icon(node)}  {c(C.GRAY, f'ip={node.ip} role={node.role} cpu={node.cpu} mem={node.memory}')}")

        node_pods = pods_by_node.get(name, [])
        if not node_pods:
            print(f"{node_pfx}{LAST}{c(C.GRAY, '(배치된 Pod 없음)')}")
        else:
            for pod_idx, pod in enumerate(node_pods):
                is_last_pod = (pod_idx == len(node_pods) - 1)
                pod_conn = node_pfx + (LAST if is_last_pod else TEE)
                pod_pfx  = node_pfx + (BLANK if is_last_pod else PIPE)
                for line in render_pod(pod, pod_conn, pod_pfx, cache):
                    print(line)

    unscheduled = pods_by_node.get("unscheduled", [])
    if unscheduled:
        print(f"{TEE}{c(C.YELLOW, 'Unscheduled Pods')}")
        for i, pod in enumerate(unscheduled):
            is_last = (i == len(unscheduled) - 1)
            for line in render_pod(pod, PIPE + (LAST if is_last else TEE),
                                   PIPE + (BLANK if is_last else PIPE), cache):
                print(line)


# ── 요약 출력 ───────────────────────────────────────────────────────────────
def print_summary(cache: ResourceCache):
    pods = cache.pods
    running   = sum(1 for p in pods if p.phase == "Running")
    pending   = sum(1 for p in pods if p.phase == "Pending")
    failed    = sum(1 for p in pods if p.phase in ("Failed", "Unknown"))
    nodes_ok  = sum(1 for n in cache.nodes if n.ready)

    print(c(C.BOLD, "\n📊 Summary"))
    print(c(C.GRAY, "─" * 50))
    print(f"  Nodes       : {c(C.GREEN, str(nodes_ok))} / {len(cache.nodes)}")
    print(f"  Pods        : {c(C.GREEN, str(running))} running  "
          f"{c(C.YELLOW, str(pending))} pending  "
          f"{c(C.RED, str(failed))} failed")
    print(f"  Deployments : {len(cache.deployments)}")
    print(f"  Services    : {len(cache.services)}")
    print(f"  PVCs        : {len(cache.pvcs)}")
    print(f"  Ingresses   : {len(cache.ingresses)}")
    print(f"  HPAs        : {len(cache.hpas)}")
    print(f"  Jobs        : {len(cache.jobs)}")
    print(f"  CronJobs    : {len(cache.cronjobs)}")
    print(f"  ConfigMaps  : {len(cache.configmaps)}")
    print(f"  Secrets     : {len(cache.secrets)}")

    if cache.pvs:
        print(f"\n  💾 PersistentVolumes ({len(cache.pvs)})")
        for pv in cache.pvs:
            icon = c(C.GREEN, "✅") if pv.status == "Bound" else c(C.YELLOW, "⚠️")
            bound = f"→ {pv.bound_pvc_namespace}/{pv.bound_pvc_name}" if pv.bound_pvc_name else "(unbound)"
            print(f"    {icon} {pv.name}  {pv.capacity}  {pv.status}  {c(C.GRAY, bound)}")
    print()


# ── 메인 ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="KubeIn K8s 리소스 ASCII 트리 출력기 (경량 데이터 모델)"
    )
    parser.add_argument("-n", "--namespace", default=None,
                        help="특정 네임스페이스만 출력 (예: -n sock-shop)")
    parser.add_argument("--no-color", action="store_true",
                        help="ANSI 색상 없이 출력")
    parser.add_argument("--nodes-only", action="store_true",
                        help="노드 뷰만 출력")
    parser.add_argument("--ns-only", action="store_true",
                        help="네임스페이스 뷰만 출력")
    parser.add_argument("--summary-only", action="store_true",
                        help="요약만 출력")
    args = parser.parse_args()

    global NO_COLOR
    NO_COLOR = args.no_color

    print(c(C.BOLD + C.CYAN, "⚙  ResourceCache 로딩 중... (K8s API 17회 호출)"), flush=True)

    try:
        cache = ResourceCache()
    except Exception as e:
        print(f"[ERROR] 클러스터 연결 실패: {e}", file=sys.stderr)
        sys.exit(1)

    if args.summary_only:
        print_summary(cache)
        return

    if not args.ns_only:
        print_node_view(cache)

    if not args.nodes_only:
        print_namespace_view(cache, ns_filter=args.namespace)

    print_summary(cache)


if __name__ == "__main__":
    main()
