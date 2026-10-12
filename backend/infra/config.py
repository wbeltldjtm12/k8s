"""
infra/config.py
Kubernetes 설정 로드 (최초 1회만 실행)
"""
import os
from pathlib import Path

from kubernetes import config

_loaded = False
DEFAULT_RUNTIME_ENV_FILE = Path("/home/master/kubein-config/.env.cluster")


def load_runtime_env(env_file: str | os.PathLike[str] | None = None) -> Path | None:
    """Load the cluster runtime environment without replacing explicit variables.

    Docker Compose injects these variables directly, while ``python main.py``
    on the master does not.  Loading the same server-side file here keeps both
    execution paths consistent and lets explicit process variables win.
    """
    configured_path = os.environ.get("KUBEIN_ENV_FILE", "").strip()
    path = Path(env_file) if env_file else (
        Path(configured_path) if configured_path else DEFAULT_RUNTIME_ENV_FILE
    )
    if not path.is_file():
        return None

    with path.open("r", encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)

    return path


def load_k8s_config():
    global _loaded
    load_runtime_env()
    if _loaded:
        return

    try:
        config.load_incluster_config()
        _loaded = True
        return
    except config.ConfigException:
        pass

    kubeconfig = os.environ.get("KUBECONFIG", "")
    if kubeconfig and os.path.isfile(kubeconfig):
        config.load_kube_config(config_file=kubeconfig)
        _loaded = True
        return

    if os.path.isfile("/tmp/kubeconfig"):
        config.load_kube_config(config_file="/tmp/kubeconfig")
        _loaded = True
        return

    config.load_kube_config()
    _loaded = True
