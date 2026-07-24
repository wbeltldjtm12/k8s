#!/usr/bin/env bash
set -eu

SOURCE_KUBECONFIG="${KUBECONFIG:-/root/.kube/config}"

if [ -f "$SOURCE_KUBECONFIG" ]; then
    cp "$SOURCE_KUBECONFIG" /tmp/kubeconfig

    if [ "${KUBEINSIGHT_ENV:-prod}" = "dev" ]; then
        sed -i '/certificate-authority-data/a\    insecure-skip-tls-verify: true' /tmp/kubeconfig
        sed -i '/certificate-authority-data/d' /tmp/kubeconfig
        echo "[*] [DEV] TLS 검증 비활성화됨"
    fi

    export KUBECONFIG=/tmp/kubeconfig
    echo "[*] kubeconfig 설정 완료 (host network mode)"
else
    echo "[!] kubeconfig를 찾을 수 없음: $SOURCE_KUBECONFIG" >&2
    exit 1
fi

echo "[*] KubeInsight 엔진 시작 중..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
