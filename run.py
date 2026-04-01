# -*- coding: utf-8 -*-
"""Run AI Interviewer Demo: FastAPI + WebSocket + Gradio at /gradio.

Optionally auto-start SoulX-Duplug VAD server (external repo) on port 7997.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn


def _env_flag(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name, "") or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def _start_duplug_if_enabled() -> subprocess.Popen | None:
    """Start SoulX-Duplug server:app as a subprocess if enabled via env."""
    if not _env_flag("DUPLUG_AUTOSTART", default=False):
        return None

    duplug_dir = (os.environ.get("DUPLUG_DIR", "") or "").strip()
    if not duplug_dir:
        print("[run.py] DUPLUG_AUTOSTART=1 but DUPLUG_DIR is empty.", file=sys.stderr)
        print("[run.py] Please set DUPLUG_DIR=/path/to/SoulX-Duplug repo root.", file=sys.stderr)
        return None

    cwd = Path(duplug_dir).expanduser().resolve()
    server_py = cwd / "server.py"
    if not server_py.exists():
        print(f"[run.py] Invalid DUPLUG_DIR (missing server.py): {cwd}", file=sys.stderr)
        return None

    duplug_port = int((os.environ.get("DUPLUG_PORT", "") or "7997").strip())
    duplug_host = (os.environ.get("DUPLUG_HOST", "") or "127.0.0.1").strip()

    # Use `python -m uvicorn` to avoid relying on PATH resolution.
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        duplug_host,
        "--port",
        str(duplug_port),
        "--workers",
        "1",
    ]

    env = os.environ.copy()
    # Ensure our backend points to the Duplug server by default when autostarting.
    env.setdefault("DUPLUG_VAD_ENABLED", "true")
    env.setdefault("DUPLUG_VAD_URL", f"ws://{duplug_host}:{duplug_port}/turn")

    print(f"[run.py] Starting Duplug VAD at {env['DUPLUG_VAD_URL']} (cwd={cwd})")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env)  # noqa: S603

    # Best-effort: wait briefly so backend doesn't race connect immediately.
    time.sleep(0.6)
    return proc


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort terminate on shutdown."""
    try:
        proc.send_signal(signal.SIGTERM)
    except Exception:
        return
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    duplug_proc: subprocess.Popen | None = _start_duplug_if_enabled()
    try:
        uvicorn.run(
            "backend.server:app",
            host="0.0.0.0",
            port=7998,
            reload=False,
        )
    finally:
        if duplug_proc is not None:
            print("[run.py] Stopping Duplug VAD subprocess...")
            _terminate_process_tree(duplug_proc)
