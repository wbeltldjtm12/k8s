"""Run one safe, local adaptive-observability experiment on Windows.

The script starts a disposable HTTP service, injects one fault, and writes all
observations under experiments/eviwatch_lab/runs/. It never touches Kubernetes,
Docker, firewall rules, or the real system disk capacity.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import Structure, byref, c_ulong, c_ulonglong
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
PORT = 18080


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FILETIME(Structure):
    _fields_ = [("dwLowDateTime", c_ulong), ("dwHighDateTime", c_ulong)]


class MEMORYSTATUSEX(Structure):
    _fields_ = [
        ("dwLength", c_ulong),
        ("dwMemoryLoad", c_ulong),
        ("ullTotalPhys", c_ulonglong),
        ("ullAvailPhys", c_ulonglong),
        ("ullTotalPageFile", c_ulonglong),
        ("ullAvailPageFile", c_ulonglong),
        ("ullTotalVirtual", c_ulonglong),
        ("ullAvailVirtual", c_ulonglong),
        ("ullAvailExtendedVirtual", c_ulonglong),
    ]


def filetime_to_int(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) + value.dwLowDateTime


class SystemSampler:
    def __init__(self) -> None:
        self.previous_cpu = self._cpu_times()

    @staticmethod
    def _cpu_times() -> tuple[int, int, int]:
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(byref(idle), byref(kernel), byref(user)):
            raise ctypes.WinError()
        return tuple(map(filetime_to_int, (idle, kernel, user)))

    def cpu_percent(self) -> float:
        current = self._cpu_times()
        previous = self.previous_cpu
        self.previous_cpu = current
        idle_delta = current[0] - previous[0]
        total_delta = (current[1] - previous[1]) + (current[2] - previous[2])
        if total_delta <= 0:
            return 0.0
        return round(100.0 * (total_delta - idle_delta) / total_delta, 2)

    @staticmethod
    def memory_available_mb() -> int:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(byref(status)):
            raise ctypes.WinError()
        return round(status.ullAvailPhys / 1024 / 1024)

    @staticmethod
    def disk_used_percent() -> float:
        drive = Path.cwd().anchor or "C:\\"
        usage = shutil.disk_usage(drive)
        return round(100.0 * usage.used / usage.total, 2)


class EventReader:
    def __init__(self, event_log: Path) -> None:
        self.event_log = event_log
        self.offset = 0

    def read_new(self) -> list[dict[str, object]]:
        if not self.event_log.exists():
            return []
        with self.event_log.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            lines = handle.readlines()
            self.offset = handle.tell()
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"timestamp": utc_now(), "level": "ERROR", "event": "invalid_log_line"})
        return events


def probe_health() -> tuple[bool, float | None, str | None]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1.0) as response:
            response.read()
            return response.status == 200, round((time.perf_counter() - started) * 1000, 2), None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return False, None, type(exc).__name__


def wait_for_health(timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        health_ok, _latency, _error = probe_health()
        if health_ok:
            return
        time.sleep(0.1)
    raise RuntimeError("The disposable demo service did not start within the timeout.")


def start_service(event_log: Path, stdout_log: Path) -> subprocess.Popen[str]:
    stdout_handle = stdout_log.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "demo_service.py"), "--port", str(PORT), "--event-log", str(event_log)],
        stdout=stdout_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # The child owns stdout_handle after spawn; retaining a reference avoids early close.
    process._eviwatch_stdout_handle = stdout_handle  # type: ignore[attr-defined]
    return process


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    handle = getattr(process, "_eviwatch_stdout_handle", None)
    if handle is not None:
        handle.close()


def write_jsonl(handle, payload: dict[str, object]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    handle.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=("fixed-high", "adaptive"), required=True)
    parser.add_argument("--fault", choices=("service-stop", "cpu-pressure"), required=True)
    parser.add_argument("--normal-seconds", type=float, default=10)
    parser.add_argument("--fault-seconds", type=float, default=12)
    parser.add_argument("--recovery-seconds", type=float, default=10)
    parser.add_argument("--cpu-workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--cpu-threshold", type=float, default=70.0)
    args = parser.parse_args()

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{args.fault}_{args.policy}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    event_log = run_dir / "service-events.jsonl"
    stdout_log = run_dir / "service-stdout.log"

    ground_truth = {
        "run_id": run_id,
        "fault": args.fault,
        "policy": args.policy,
        "expected_evidence": {
            "service-stop": ["health_ok=false", "health_error", "service_stopped"],
            "cpu-pressure": ["cpu_percent increase", "policy transition or high CPU samples"],
        }[args.fault],
    }
    (run_dir / "ground_truth.json").write_text(json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8")

    sampler = SystemSampler()
    reader = EventReader(event_log)
    service = start_service(event_log, stdout_log)
    wait_for_health()
    burner: subprocess.Popen[str] | None = None
    fault_started = False
    recovered = False
    state = "NORMAL"
    incident_until = 0.0
    started = time.monotonic()
    fault_at = args.normal_seconds
    recovery_at = args.normal_seconds + args.fault_seconds
    finished_at = recovery_at + args.recovery_seconds
    previous_sample = started - 1.0

    fields = [
        "timestamp", "elapsed_s", "phase", "policy_state", "cpu_percent", "memory_available_mb",
        "disk_used_percent", "health_ok", "health_latency_ms", "health_error", "new_events_seen",
        "new_events_retained",
    ]
    try:
        with (run_dir / "metrics.csv").open("w", newline="", encoding="utf-8") as metrics_file, \
             (run_dir / "policy-events.jsonl").open("w", encoding="utf-8") as policy_file, \
             (run_dir / "retained-events.jsonl").open("w", encoding="utf-8") as retained_file:
            writer = csv.DictWriter(metrics_file, fieldnames=fields)
            writer.writeheader()
            write_jsonl(policy_file, {"timestamp": utc_now(), "event": "trial_started", "state": state})

            while True:
                now = time.monotonic()
                elapsed = now - started
                if elapsed >= finished_at:
                    break

                if not fault_started and elapsed >= fault_at:
                    fault_started = True
                    if args.fault == "service-stop":
                        stop_process(service)
                    else:
                        burner = subprocess.Popen(
                            [sys.executable, str(ROOT / "cpu_burner.py"), "--seconds", str(args.fault_seconds),
                             "--workers", str(args.cpu_workers)],
                            text=True,
                        )
                    write_jsonl(policy_file, {"timestamp": utc_now(), "event": "fault_injected", "fault": args.fault})

                if fault_started and not recovered and elapsed >= recovery_at:
                    recovered = True
                    if args.fault == "service-stop":
                        service = start_service(event_log, stdout_log)
                    write_jsonl(policy_file, {"timestamp": utc_now(), "event": "fault_recovery_started", "fault": args.fault})

                sample_interval = 1.0 if args.policy == "fixed-high" or state == "INCIDENT" else 5.0
                if now - previous_sample < sample_interval:
                    time.sleep(0.05)
                    continue
                previous_sample = now

                health_ok, health_latency, health_error = probe_health()
                cpu_percent = sampler.cpu_percent()
                events = reader.read_new()
                trigger_reason: str | None = None
                if not health_ok:
                    trigger_reason = "health_failed"
                elif cpu_percent >= args.cpu_threshold:
                    trigger_reason = "cpu_threshold"

                if args.policy == "adaptive" and trigger_reason and state != "INCIDENT":
                    state = "INCIDENT"
                    incident_until = now + args.fault_seconds + args.recovery_seconds
                    write_jsonl(policy_file, {"timestamp": utc_now(), "event": "policy_transition", "from": "NORMAL", "to": "INCIDENT", "reason": trigger_reason})
                elif args.policy == "adaptive" and state == "INCIDENT" and now >= incident_until and health_ok:
                    state = "NORMAL"
                    write_jsonl(policy_file, {"timestamp": utc_now(), "event": "policy_transition", "from": "INCIDENT", "to": "NORMAL", "reason": "recovered"})

                retain_all = args.policy == "fixed-high" or state == "INCIDENT"
                retained = [event for event in events if retain_all or event.get("level") in {"WARN", "ERROR"}]
                for event in retained:
                    write_jsonl(retained_file, event)

                phase = "normal" if elapsed < fault_at else "fault" if elapsed < recovery_at else "recovery"
                writer.writerow({
                    "timestamp": utc_now(),
                    "elapsed_s": round(elapsed, 2),
                    "phase": phase,
                    "policy_state": state,
                    "cpu_percent": cpu_percent,
                    "memory_available_mb": sampler.memory_available_mb(),
                    "disk_used_percent": sampler.disk_used_percent(),
                    "health_ok": health_ok,
                    "health_latency_ms": health_latency if health_latency is not None else "",
                    "health_error": health_error or "",
                    "new_events_seen": len(events),
                    "new_events_retained": len(retained),
                })
                metrics_file.flush()
                print(f"[{phase:8}] state={state:8} cpu={cpu_percent:5.1f}% health={health_ok} retained={len(retained)}")
    finally:
        stop_process(service)
        stop_process(burner)

    output_sizes = {path.name: path.stat().st_size for path in run_dir.iterdir() if path.is_file()}
    summary = {"run_id": run_id, "fault": args.fault, "policy": args.policy, "files_bytes": output_sizes}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nCompleted: {run_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
