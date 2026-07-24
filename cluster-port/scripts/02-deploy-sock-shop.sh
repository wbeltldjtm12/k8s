#!/usr/bin/env bash
# 02-deploy-sock-shop.sh
# sock-shop을 클러스터 전체에 배포
# master에서 1회 실행

set -euo pipefail

NS="sock-shop"
MANIFEST_URL="https://raw.githubusercontent.com/microservices-demo/microservices-demo/master/deploy/kubernetes/complete-demo.yaml"
MANIFEST_LOCAL="$(dirname "$0")/../sock-shop-manifests/complete-demo.yaml"

echo "================================================="
echo " sock-shop 클러스터 배포"
echo " 대상 네임스페이스: $NS"
echo "================================================="

# 네임스페이스 보장
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# ── 매니페스트 확보 ─────────────────────────────────────
if [ -f "$MANIFEST_LOCAL" ]; then
  echo "[INFO] 로컬 매니페스트 사용: $MANIFEST_LOCAL"
  MANIFEST="$MANIFEST_LOCAL"
else
  echo "[다운로드] 공식 sock-shop 매니페스트 다운로드 중..."
  mkdir -p "$(dirname "$0")/../sock-shop-manifests"
  curl -fsSL "$MANIFEST_URL" -o "$MANIFEST_LOCAL"
  MANIFEST="$MANIFEST_LOCAL"
  echo "[OK] 다운로드 완료"
fi

# ── 배포 ───────────────────────────────────────────────
kubectl apply -f "$MANIFEST"

echo ""
echo "[대기] sock-shop Pod 준비 중... (최대 5분)"
kubectl rollout status deployment --all -n "$NS" --timeout=300s 2>/dev/null || true

# ── 상태 확인 ──────────────────────────────────────────
echo ""
echo "[현재 Pod 상태]"
kubectl get pods -n "$NS" -o wide

BAD=$(kubectl get pods -n "$NS" --no-headers 2>/dev/null \
  | awk '{n=split($2, ready, "/"); if (n != 2 || ready[2] == 0 || ready[1] != ready[2] || $3 != "Running") print $1}')
POD_COUNT=$(kubectl get pods -n "$NS" --no-headers 2>/dev/null | wc -l | tr -d ' ')

if [ -z "${BAD:-}" ] && [ "${POD_COUNT:-0}" -ge 1 ]; then
  echo ""
  echo "[OK] 모든 Pod 정상 Running"
else
  echo ""
  echo "[경고] 아직 준비 안 된 Pod:"
  echo "$BAD" | sed 's/^/  - /'
  echo "(정상 기동 후 다시 실행하세요.)"
  exit 1
fi

echo ""
echo "================================================="
echo " 완료! 다음: bash cluster-port/scripts/03-pre-check-cluster.sh"
echo "================================================="
