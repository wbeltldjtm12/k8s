#!/usr/bin/env bash
# 01-install-prereqs.sh
# master 노드에서 1회만 실행
# - Docker / docker-compose 확인
# - kubectl 확인
# - Chaos Mesh 설치 (없으면)
# - helm 확인

set -euo pipefail

echo "================================================="
echo " KubeIn 클러스터 사전 요구사항 설치"
echo "================================================="

# ── 1. Docker ──────────────────────────────────────────
if command -v docker &>/dev/null; then
  echo "[OK] Docker: $(docker --version)"
else
  echo "[설치] Docker 설치 중..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "[OK] Docker 설치 완료. 재로그인 후 권한 적용됨."
fi

# ── 2. docker-compose ──────────────────────────────────
if docker compose version &>/dev/null 2>&1; then
  echo "[OK] docker compose (plugin): $(docker compose version)"
elif command -v docker-compose &>/dev/null; then
  echo "[OK] docker-compose: $(docker-compose --version)"
else
  echo "[설치] docker-compose plugin 설치 중..."
  sudo apt-get install -y docker-compose-plugin 2>/dev/null \
    || pip install docker-compose
fi

# ── 3. kubectl ─────────────────────────────────────────
if command -v kubectl &>/dev/null; then
  echo "[OK] kubectl: $(kubectl version --client --short 2>/dev/null || kubectl version --client)"
else
  echo "[FAIL] kubectl 없음. 클러스터 셋업 후 다시 실행하세요."
  exit 1
fi

# ── 4. kubeconfig 확인 ─────────────────────────────────
if kubectl cluster-info &>/dev/null; then
  NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
  echo "[OK] 클러스터 연결 정상 (노드 ${NODE_COUNT}개)"
  kubectl get nodes -o wide
else
  echo "[FAIL] kubectl이 클러스터에 연결 안 됨. kubeconfig 확인 필요."
  exit 1
fi

# ── 5. Chaos Mesh ──────────────────────────────────────
if kubectl get namespace chaos-mesh &>/dev/null; then
  echo "[OK] Chaos Mesh 네임스페이스 존재"
  kubectl get pods -n chaos-mesh --no-headers 2>/dev/null | head -5
else
  echo "[설치] Chaos Mesh 설치 중..."

  # helm 확인
  if ! command -v helm &>/dev/null; then
    echo "[설치] helm 설치 중..."
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  fi

  helm repo add chaos-mesh https://charts.chaos-mesh.org
  helm repo update

  kubectl create namespace chaos-mesh

  # containerd 사용 클러스터 (kubeadm 기본)
  helm install chaos-mesh chaos-mesh/chaos-mesh \
    --namespace chaos-mesh \
    --set chaosDaemon.runtime=containerd \
    --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
    --version 2.6.3

  echo "[대기] Chaos Mesh Pod 준비 중..."
  kubectl wait --for=condition=Ready pod \
    --all -n chaos-mesh --timeout=120s

  echo "[OK] Chaos Mesh 설치 완료"
fi

# ── 6. sock-shop 네임스페이스 ──────────────────────────
if kubectl get namespace sock-shop &>/dev/null; then
  echo "[OK] sock-shop 네임스페이스 존재"
else
  echo "[생성] sock-shop 네임스페이스 생성"
  kubectl create namespace sock-shop
fi

echo ""
echo "================================================="
echo " 사전 요구사항 확인 완료!"
echo " 다음: bash 02-deploy-sock-shop.sh"
echo "================================================="
