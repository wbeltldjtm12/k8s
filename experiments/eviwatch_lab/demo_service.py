"""A tiny HTTP service used only for EviWatch fault-injection experiments."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DemoHandler(BaseHTTPRequestHandler):
    event_log: Path

    def log_event(self, level: str, event: str, **details: object) -> None:
        payload = {"timestamp": utc_now(), "level": level, "event": event, **details}
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            self.log_event("WARN", "unknown_path", path=self.path)
            return

        body = json.dumps({"status": "ok", "timestamp": utc_now()}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.log_event("INFO", "health_ok")

    def log_message(self, _format: str, *_args: object) -> None:
        # HTTP access logs are intentionally written through log_event instead.
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--event-log", required=True)
    args = parser.parse_args()

    event_log = Path(args.event_log)
    event_log.parent.mkdir(parents=True, exist_ok=True)
    DemoHandler.event_log = event_log

    server = ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler)
    DemoHandler.log_event(DemoHandler, "INFO", "service_started", port=args.port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        DemoHandler.log_event(DemoHandler, "INFO", "service_stopped")
        server.server_close()


if __name__ == "__main__":
    main()
