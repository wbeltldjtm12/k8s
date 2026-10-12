# KubeIn 네트워크 관측 probe (실험 계층)

Kubernetes API와 node-level Prometheus 메트릭만으로는 Pod 내부 DNS 실패,
서비스 요청 지연, 네트워크 손실을 구분할 수 없다. 이 디렉터리는 그 빈 자리를
메우기 위한 작은 **sidecar probe exporter**다.

이 probe는 대상 애플리케이션 Pod와 네트워크 namespace를 공유한다. 따라서 Chaos
Mesh가 그 Pod에 DNS/Network fault를 적용하면 probe도 같은 실패를 관측한다.

```text
shipping/payment/front-end Pod
├─ application container
└─ kubein-netprobe sidecar
      ├─ DNS lookup / HTTP request
      └─ /metrics ── PodMonitor ── Prometheus ── KubeIn
```

> 이 구성은 원 논문 KubeIn의 agent-free baseline을 바꾸지 않는다. 네트워크
> 관측 확장 실험에서만 별도 설치한다.

## 먼저 이미지 빌드·push

```bash
cd ~/kubein/cluster-port/network-probe
TAG=network-observability-20260730
docker build -t "sch02/kubein-network-probe:${TAG}" .
docker push "sch02/kubein-network-probe:${TAG}"
docker buildx imagetools inspect "sch02/kubein-network-probe:${TAG}"
```

배포에는 위 출력의 registry digest를 사용한다. 같은 tag를 재사용하지 않는다.
private registry라면 먼저 실험 namespace에 전용 pull secret을 만들고 설치 시 그
이름을 `PROBE_IMAGE_PULL_SECRET`으로 넘긴다. 스크립트는 secret의 존재를 모든
Deployment 변경 전에 확인한다.

## 설치와 확인

```bash
cd ~/kubein
PROBE_IMAGE=sch02/kubein-network-probe@sha256:<registry-digest> \
PROBE_IMAGE_PULL_SECRET=<registry-secret-name> \
  bash cluster-port/network-probe/install.sh

kubectl get pods -n sock-shop -l kubein.io/network-probe=true
kubectl get podmonitor -n monitoring kubein-network-probe
```

Prometheus가 scrape를 시작한 뒤 다음 PromQL이 하나 이상의 시계열을 반환해야 한다.

```promql
kubein_network_probe_success{namespace="sock-shop"}
```

백엔드에는 다음 값을 함께 설정한다.

```dotenv
KUBEIN_NETWORK_EVIDENCE_ENABLED=true
KUBEIN_NETWORK_EVIDENCE_REQUIRED=true
KUBEIN_NETWORK_PROBE_EXPECTED_WORKLOADS=shipping,payment,front-end
KUBEIN_NETWORK_PROBE_WINDOW_SECONDS=45
KUBEIN_NETWORK_PROBE_MAX_SAMPLE_AGE_SECONDS=10
KUBEIN_NETWORK_PROBE_MAX_OBSERVATION_AGE_SECONDS=8
KUBEIN_NETWORK_PROBE_MIN_SAMPLES=2
```

DNS error(11번), network delay(12번), network loss(13번)를 주입한 뒤에는 같은
시계열의 실패 횟수 또는 지연 증가가 45초 관측창에 남는다. 13번의 100% egress
loss 동안에는 Prometheus scrape도 실패하므로 백엔드는 snapshot 생성을 503으로
거부한다. fault 종료 뒤 exporter가 다시 scrape되고 실패 counter가 보이면
`HTTPProbeFailed`로 판정한다. `up=0` 자체를 HTTP 원인으로 오인하지 않는다.

평가할 때는 기존 `eval/ground_truth.json`이 아니라 별도 프로필
`eval/ground_truth.network-extended.json`을 명시한다. 이 프로필은 11–13번만
관측 가능 상태로 바꾸며 기존 baseline GT는 수정하지 않는다.

- 11: `shipping->catalogue[dns]` / `DNSProbeFailed`
- 12: `payment->catalogue[http]` / `HighNetworkLatency`
- 13: `front-end->front-end[http]` / `HTTPProbeFailed`

## 복구

```bash
PROBE_IMAGE_PULL_SECRET=<registry-secret-name> \
  bash cluster-port/network-probe/uninstall.sh
```

이 명령은 `kubein-netprobe` sidecar, 해당 pull-secret 참조, 실험용 PodMonitor를
제거한다. Secret 객체 자체는 설치 스크립트가 만들지 않으므로 별도로 삭제한다.
