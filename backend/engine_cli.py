#!/usr/bin/env python3
"""Run the KUBEIN analysis engine directly, without HTTP or Uvicorn."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_SERVER_ENV = Path("/home/master/kubein-config/.env.cluster")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "KUBEIN 엔진을 FastAPI 없이 한 번 실행하고 재현 가능한 JSON 결과를 저장합니다."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("full", "hybrid", "dfs_only", "llm_only", "all"),
        default="full",
        help=(
            "full은 기존 python main.py 파이프라인, all은 동일 snapshot으로 "
            "hybrid/dfs_only/llm_only를 모두 실행합니다."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "KEY=VALUE 환경 파일. 미지정 시 KUBEIN_ENV_FILE 또는 "
            "/home/master/kubein-config/.env.cluster를 사용합니다."
        ),
    )
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        help="사용할 kubeconfig 경로. 미지정 시 기존 KUBECONFIG를 유지합니다.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="결과 JSON 경로. 미지정 시 engine-results/ 아래에 자동 생성합니다.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding explicit OS variables."""
    with path.open("r", encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def resolve_env_file(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit.expanduser().resolve()
    configured = os.environ.get("KUBEIN_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if DEFAULT_SERVER_ENV.is_file():
        return DEFAULT_SERVER_ENV
    return None


def normalize_response(value: Any) -> tuple[dict[str, Any], int]:
    """Convert either a normal endpoint dict or FastAPI JSONResponse to a dict."""
    if isinstance(value, dict):
        return value, 200

    body = getattr(value, "body", None)
    status_code = int(getattr(value, "status_code", 500))
    if isinstance(body, bytes):
        decoded = json.loads(body.decode("utf-8"))
        if isinstance(decoded, dict):
            return decoded, status_code
    raise TypeError(f"지원하지 않는 엔진 응답 형식: {type(value).__name__}")


def current_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def default_output_path(mode: str, run_id: str) -> Path:
    result_root = Path(
        os.environ.get("KUBEIN_RESULTS_DIR", str(REPO_ROOT / "engine-results"))
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return result_root / f"{timestamp}_{mode}_{run_id[:8]}.json"


def execute_mode(mode: str) -> dict[str, Any]:
    # Environment must be loaded before importing main because its LLM,
    # retriever, Kubernetes config, and Prometheus defaults initialize at import.
    from main import (  # pylint: disable=import-outside-toplevel
        create_evaluation_snapshot,
        evaluate,
        release_evaluation_snapshot,
        run_analysis,
    )

    if mode == "full":
        return {
            "status": "success",
            "mode": "full",
            "data": run_analysis(),
        }

    modes = ("hybrid", "dfs_only", "llm_only") if mode == "all" else (mode,)
    snapshot_payload, snapshot_status = normalize_response(
        create_evaluation_snapshot()
    )
    if snapshot_status >= 400 or snapshot_payload.get("status") != "success":
        raise RuntimeError(
            f"평가 snapshot 생성 실패: {json.dumps(snapshot_payload, ensure_ascii=False)}"
        )

    snapshot_id = str(snapshot_payload["snapshot_id"])
    results: dict[str, Any] = {}
    try:
        for selected_mode in modes:
            response, status_code = normalize_response(
                evaluate(mode=selected_mode, snapshot_id=snapshot_id)
            )
            if status_code >= 400 or response.get("status") != "success":
                raise RuntimeError(
                    f"{selected_mode} 실행 실패: "
                    f"{json.dumps(response, ensure_ascii=False)}"
                )
            results[selected_mode] = response
    finally:
        release_evaluation_snapshot(snapshot_id)

    return {
        "status": "success",
        "mode": mode,
        "snapshot": snapshot_payload,
        "results": results,
    }


def main() -> int:
    args = parse_args()
    env_file = resolve_env_file(args.env_file)
    if env_file:
        if not env_file.is_file():
            print(f"[ERROR] 환경 파일을 찾을 수 없습니다: {env_file}", file=sys.stderr)
            return 2
        load_env_file(env_file)

    if args.kubeconfig:
        kubeconfig = args.kubeconfig.expanduser().resolve()
        if not kubeconfig.is_file():
            print(f"[ERROR] kubeconfig를 찾을 수 없습니다: {kubeconfig}", file=sys.stderr)
            return 2
        os.environ["KUBECONFIG"] = str(kubeconfig)

    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else default_output_path(args.mode, run_id)
    )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "mode": args.mode,
        "git_commit": current_commit(),
        "environment": {
            "env_file": str(env_file) if env_file else "",
            "kubeconfig": os.environ.get("KUBECONFIG", ""),
            "llm_model": os.environ.get("LLM_MODEL", ""),
            "prometheus_url": os.environ.get("PROMETHEUS_URL", ""),
        },
    }

    exit_code = 0
    try:
        payload["engine"] = execute_mode(args.mode)
        payload["status"] = "success"
    except Exception as exc:  # Preserve a machine-readable failed experiment.
        payload["status"] = "error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1

    completed_at = datetime.now(timezone.utc)
    payload["completed_at"] = completed_at.isoformat()
    payload["duration_sec"] = round(time.monotonic() - started_monotonic, 3)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)

    print(f"[RESULT] {output_path}")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "mode": args.mode,
                "duration_sec": payload["duration_sec"],
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
