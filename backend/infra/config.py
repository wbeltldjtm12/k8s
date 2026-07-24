"""
infra/config.py
Kubernetes 설정 로드 (최초 1회만 실행)
"""
import os
from kubernetes import config

_loaded = False


def load_k8s_config():
    global _loaded
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
