# Manual Prometheus management

Prometheus is deliberately managed from the Kubernetes master instead of the
application CI/CD workflow.

Install or upgrade the pinned stack:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts \
  --force-update
helm repo update

helm upgrade --install monitoring \
  prometheus-community/kube-prometheus-stack \
  --version 86.0.0 \
  --namespace monitoring \
  --create-namespace \
  --values cluster-port/monitoring-values.yaml \
  --atomic \
  --timeout 10m
```

Verify:

```bash
kubectl get pods -n monitoring -o wide
kubectl get services -n monitoring
curl --connect-timeout 5 --max-time 10 \
  -fsS http://192.168.0.12:30090/-/ready
```

KUBEIN uses:

```dotenv
PROMETHEUS_URL=http://192.168.0.12:30090
```
