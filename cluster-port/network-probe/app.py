#!/usr/bin/env python3
"""Small Prometheus exporter for in-Pod DNS and HTTP reachability probes.

The process is intended to run as a sidecar.  It shares the application's
network namespace, so a Chaos Mesh DNS/Network fault affecting the application
Pod also affects this probe.  This is intentionally an experimental
observability component; it is not part of the published agent-free KubeIn
baseline.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable


METRICS_PORT = int(os.environ.get("METRICS_PORT", "9155"))
PROBE_INTERVAL_SECONDS = max(float(os.environ.get("PROBE_INTERVAL_SECONDS", "5")), 1.0)
PROBE_TIMEOUT_SECONDS = max(float(os.environ.get("PROBE_TIMEOUT_SECONDS", "2")), 0.1)
SOURCE_WORKLOAD = os.environ.get("PROBE_SOURCE_WORKLOAD", "unknown")
SOURCE_POD = os.environ.get("POD_NAME", "unknown")
NAMESPACE = os.environ.get("POD_NAMESPACE", "default")


@dataclass(frozen=True)
class ProbeTarget:
    """One configured target; ``name`` becomes a stable Prometheus label."""

    name: str
    value: str


@dataclass(frozen=True)
class ProbeResult:
    """A single DNS or HTTP attempt."""

    probe: str
    target: str
    success: bool
    latency_seconds: float
    observed_at: float
    status_code: int = 0


def parse_targets(value: str) -> list[ProbeTarget]:
    """Parse ``name=value,name=value`` while allowing a bare target name."""
    targets: list[ProbeTarget] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        name, separator, target_value = item.partition("=")
        targets.append(
            ProbeTarget(
                name=name.strip(),
                value=(target_value if separator else name).strip(),
            )
        )
    return targets


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class ProbeStore:
    """Thread-safe latest values and monotonically increasing failure counts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[tuple[str, str], ProbeResult] = {}
        self._failures: dict[tuple[str, str], int] = {}

    def record(self, result: ProbeResult) -> None:
        key = (result.probe, result.target)
        with self._lock:
            self._latest[key] = result
            if not result.success:
                self._failures[key] = self._failures.get(key, 0) + 1

    def render_prometheus(self) -> str:
        labels_base = {
            "namespace": NAMESPACE,
            "source_workload": SOURCE_WORKLOAD,
            "source_pod": SOURCE_POD,
        }
        lines = [
            "# HELP kubein_network_probe_success Latest probe success (1=reachable, 0=failed).",
            "# TYPE kubein_network_probe_success gauge",
            "# HELP kubein_network_probe_latency_seconds Latest probe duration in seconds.",
            "# TYPE kubein_network_probe_latency_seconds gauge",
            "# HELP kubein_network_probe_observed_timestamp_seconds Unix timestamp of the latest completed probe.",
            "# TYPE kubein_network_probe_observed_timestamp_seconds gauge",
            "# HELP kubein_network_probe_failures_total Total failed probe attempts.",
            "# TYPE kubein_network_probe_failures_total counter",
            "# HELP kubein_network_probe_http_status_code Latest HTTP response code (0=no response).",
            "# TYPE kubein_network_probe_http_status_code gauge",
        ]
        with self._lock:
            latest = dict(self._latest)
            failures = dict(self._failures)

        for (probe, target), result in sorted(latest.items()):
            labels = {**labels_base, "probe": probe, "target": target}
            rendered_labels = ",".join(
                f'{key}="{escape_label(value)}"' for key, value in sorted(labels.items())
            )
            lines.append(
                f"kubein_network_probe_success{{{rendered_labels}}} {1 if result.success else 0}"
            )
            lines.append(
                f"kubein_network_probe_latency_seconds{{{rendered_labels}}} {result.latency_seconds:.6f}"
            )
            lines.append(
                f"kubein_network_probe_observed_timestamp_seconds{{{rendered_labels}}} "
                f"{result.observed_at:.6f}"
            )
            lines.append(
                f"kubein_network_probe_failures_total{{{rendered_labels}}} {failures.get((probe, target), 0)}"
            )
            if probe == "http":
                lines.append(
                    f"kubein_network_probe_http_status_code{{{rendered_labels}}} {result.status_code}"
                )
        return "\n".join(lines) + "\n"


def probe_dns(target: ProbeTarget, timeout: float) -> ProbeResult:
    started = time.monotonic()
    previous_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(target.value, None)
        return ProbeResult(
            "dns",
            target.name,
            True,
            time.monotonic() - started,
            time.time(),
        )
    except OSError:
        return ProbeResult(
            "dns",
            target.name,
            False,
            time.monotonic() - started,
            time.time(),
        )
    finally:
        socket.setdefaulttimeout(previous_timeout)


def probe_http(target: ProbeTarget, timeout: float) -> ProbeResult:
    started = time.monotonic()
    request = urllib.request.Request(target.value, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ProbeResult(
                "http",
                target.name,
                True,
                time.monotonic() - started,
                time.time(),
                response.status,
            )
    except urllib.error.HTTPError as error:
        # A received HTTP response, including 4xx/5xx, proves network reachability.
        return ProbeResult(
            "http",
            target.name,
            True,
            time.monotonic() - started,
            time.time(),
            error.code,
        )
    except (OSError, ValueError, urllib.error.URLError):
        return ProbeResult(
            "http",
            target.name,
            False,
            time.monotonic() - started,
            time.time(),
        )


def run_probe_loop(
    store: ProbeStore,
    dns_targets: Iterable[ProbeTarget],
    http_targets: Iterable[ProbeTarget],
    stop_event: threading.Event,
    interval_seconds: float = PROBE_INTERVAL_SECONDS,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> None:
    while not stop_event.is_set():
        for target in dns_targets:
            store.record(probe_dns(target, timeout_seconds))
        for target in http_targets:
            store.record(probe_http(target, timeout_seconds))
        stop_event.wait(interval_seconds)


def build_handler(store: ProbeStore) -> type[BaseHTTPRequestHandler]:
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/healthz":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            elif self.path == "/metrics":
                body = store.render_prometheus().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
            else:
                body = b"not found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return MetricsHandler


def main() -> None:
    store = ProbeStore()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_probe_loop,
        args=(
            store,
            parse_targets(os.environ.get("DNS_TARGETS", "")),
            parse_targets(os.environ.get("HTTP_TARGETS", "")),
            stop_event,
        ),
        daemon=True,
    )
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), build_handler(store))
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
