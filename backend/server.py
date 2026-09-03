# -*- coding: utf-8 -*-
"""
Backend: WebSocket server that bridges browser to Doubao Realtime API.
Supports end-to-end and semi end-to-end modes, barge-in, dynamic prompt.
"""
from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

try:
    import soxr
except Exception:  # noqa: BLE001
    soxr = None

from backend.audio_stream import pcm_chunks_from_bytes
from backend.latency_logger import LatencyLogger
from backend.prompt_manager import PromptManager
from backend.duplex_client import DuplexVoiceClient
from backend.followup_client import FollowupQuestionClient
from backend.omni_bridge import OmniRealtimeBridge
from backend.self_omni_bridge import SelfDeployedOmniBridge
from backend.asr_client import (
    VolcAsrClient,
    VolcAsrStreamingSession,
    DEFAULT_ASR_RESOURCE_ID,
    CHUNK_BYTES as ASR_CHUNK_BYTES,
    asr_error_hint,
)
from backend.livekit_turn_stt import (
    create_livekit_turn_stt_session,
    livekit_turn_stt_display_label,
    resolve_livekit_turn_stt_backend,
)
from backend.duplug_vad_local import duplug_local_config_path, duplug_local_enabled
from backend.vad import (
    SEMANTIC_VAD_SOURCES,
    VadEvent,
    create_vad_backend,
    shutdown_duplug_local_manager,
    vad_provider,
    vad_status_on_init_failure,
)
from backend.vad.config import resolve_vad_provider
from backend.hush import (
    create_hush_session,
    hush_enabled,
    hush_status_payload,
    init_hush_manager,
    shutdown_hush_manager,
)
from backend.hush.manager import hush_manager_error
from backend.demo_asr_broadcast import (
    DEMO_ASR_EVENT_TYPES,
    demo_asr_last_event,
    register_demo_asr_viewer,
    schedule_demo_asr_broadcast,
    unregister_demo_asr_viewer,
)
from backend.outbound.routes import router as partner_outbound_router
from backend.prod.routes import router as prod_router
from backend.ia.routes import router as ia_algo_router
from backend.analytics.routes import router as analytics_router

LOG = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if vad_provider() == "duplug" and duplug_local_enabled():
        init_duplug_local_manager(duplug_local_config_path())
    init_hush_manager()
    _startup_t2sbg_monitor_notice()
    from backend.prod.harness import start_harness_scheduler, stop_harness_scheduler

    start_harness_scheduler()
    try:
        from backend.analytics.store import get_analytics_store

        get_analytics_store().maybe_purge_expired(force=True)
    except Exception:
        LOG.exception("analytics startup purge failed")
    yield
    await stop_harness_scheduler()
    shutdown_hush_manager()
    shutdown_duplug_local_manager()


app = FastAPI(title="AI Interviewer Realtime Voice Demo", lifespan=lifespan)
app.include_router(partner_outbound_router)
app.include_router(prod_router, prefix="/prod")
# tmp-prod deploy / bookmarks keep working; same handlers as /prod.
app.include_router(prod_router, prefix="/tmp-prod")
app.include_router(ia_algo_router)
app.include_router(analytics_router)

VAD_ASR_TARGET_SAMPLE_RATE = 16000



def _load_doubao_api_key() -> str:
    """Load Doubao API Key for duplex 3.0 (X-Api-Key) from env or config.json."""
    api_key = os.environ.get("DOUBAO_API_KEY", "") or ""
    if api_key:
        return api_key
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            doubao = data.get("doubao", {}) or {}
            api_key = doubao.get("api_key", "") or ""
    except Exception:
        pass
    return api_key


def _load_doubao_duplex_vad_config() -> tuple[bool, int]:
    """
    Duplex 3.0 server-side VAD defaults.

    Returns (duplex_server_vad, end_smooth_window_ms).
  """
    server_vad = True
    end_smooth_ms = 1500
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            doubao = data.get("doubao", {}) or {}
            if "duplex_server_vad" in doubao:
                server_vad = _parse_bool(doubao.get("duplex_server_vad"), default=server_vad)
            if "end_smooth_window_ms" in doubao:
                end_smooth_ms = int(doubao.get("end_smooth_window_ms") or end_smooth_ms)
    except Exception:
        pass
    env_sv = os.environ.get("DOUBAO_DUPLEX_SERVER_VAD")
    if env_sv is not None:
        server_vad = _parse_bool(env_sv, default=server_vad)
    env_es = os.environ.get("DOUBAO_END_SMOOTH_WINDOW_MS")
    if env_es is not None and str(env_es).strip():
        try:
            end_smooth_ms = int(env_es)
        except ValueError:
            pass
    end_smooth_ms = max(200, min(5000, end_smooth_ms))
    return server_vad, end_smooth_ms


def _resolve_duplex_server_vad(cfg: dict[str, Any], default: bool) -> bool:
    """Client cfg overrides config.json default for volcano duplex server VAD."""
    if "duplex_server_vad" in cfg:
        return _parse_bool(cfg.get("duplex_server_vad"), default=default)
    volcano_vad = str(cfg.get("volcano_vad") or cfg.get("vad_mode") or "").strip().lower()
    if volcano_vad in {"server", "semantic_vad", "duplex", "server_vad", "duplex_server_vad"}:
        return True
    if volcano_vad in {"local", "smart_turn_v3", "livekit_turn_detector", "client"}:
        return False
    return default


def _load_doubao_credentials() -> tuple[str, str]:
    """Load Doubao APP_ID and ACCESS_KEY (ASR / legacy) from env or config.json."""
    app_id = os.environ.get("DOUBAO_APP_ID", "") or ""
    access_key = os.environ.get("DOUBAO_ACCESS_KEY", "") or ""

    if app_id and access_key:
        return app_id, access_key

    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            doubao = data.get("doubao", {}) or {}
            app_id = app_id or doubao.get("app_id", "") or ""
            access_key = access_key or doubao.get("access_key", "") or ""
    except Exception:
        # Do not crash on config read errors; fall back to empty.
        pass

    return app_id, access_key


def _load_asr_credentials(app_id: str, access_key: str) -> tuple[str, str]:
    """ASR 专用凭证。若 config.json 有 asr_app_id/asr_access_key 则优先使用。"""
    asr_app = os.environ.get("ASR_APP_ID", "") or ""
    asr_key = os.environ.get("ASR_ACCESS_KEY", "") or ""
    if asr_app and asr_key:
        return asr_app, asr_key
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            doubao = data.get("doubao", {}) or {}
            asr_app = asr_app or doubao.get("asr_app_id", "") or ""
            asr_key = asr_key or doubao.get("asr_access_key", "") or ""
    except Exception:
        pass
    if asr_app and asr_key:
        return asr_app, asr_key
    return app_id, access_key


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _load_t2s_debug_audio_config() -> tuple[bool, str]:
    """
    Load t2s debug audio dump config from config.json with env override.

    Priority:
    1) Env: T2S_DEBUG_AUDIO_DUMP / T2S_DEBUG_AUDIO_DIR
    2) config.json: debug.t2s_audio_dump_enabled / debug.t2s_audio_dir
    3) defaults
    """
    default_dir = str(Path(__file__).resolve().parent.parent / "debug_audio")
    enabled = False
    out_dir = default_dir
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            debug_cfg = data.get("debug", {}) or {}
            enabled = _parse_bool(debug_cfg.get("t2s_audio_dump_enabled", False), default=False)
            out_dir = str(debug_cfg.get("t2s_audio_dir", out_dir) or out_dir)
    except Exception:
        pass

    env_enabled = os.environ.get("T2S_DEBUG_AUDIO_DUMP")
    if env_enabled is not None:
        enabled = _parse_bool(env_enabled, default=enabled)
    env_dir = os.environ.get("T2S_DEBUG_AUDIO_DIR")
    if env_dir:
        out_dir = env_dir
    return enabled, out_dir


def _load_t2s_asr_keepalive_config() -> tuple[float, float]:
    """
    t2s 流式 ASR 静音 keepalive（防 45000081 等包超时）。

    Priority:
    1) Env: T2S_ASR_KEEPALIVE_IDLE_SEC / T2S_ASR_KEEPALIVE_TICK_SEC
    2) config.json: debug.t2s_asr_keepalive_idle_sec / debug.t2s_asr_keepalive_tick_sec
    3) defaults: idle 0.9s, tick 0.35s
    """
    idle = 0.9
    tick = 0.35
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            debug_cfg = data.get("debug", {}) or {}
            if "t2s_asr_keepalive_idle_sec" in debug_cfg:
                idle = float(debug_cfg["t2s_asr_keepalive_idle_sec"])
            if "t2s_asr_keepalive_tick_sec" in debug_cfg:
                tick = float(debug_cfg["t2s_asr_keepalive_tick_sec"])
    except Exception:
        pass
    env_idle = os.environ.get("T2S_ASR_KEEPALIVE_IDLE_SEC")
    if env_idle is not None and str(env_idle).strip():
        try:
            idle = float(env_idle)
        except ValueError:
            pass
    env_tick = os.environ.get("T2S_ASR_KEEPALIVE_TICK_SEC")
    if env_tick is not None and str(env_tick).strip():
        try:
            tick = float(env_tick)
        except ValueError:
            pass
    idle = max(0.1, idle)
    tick = max(0.05, tick)
    return idle, tick


def _load_t2s_pre_roll_and_speak_grace_ms() -> tuple[int, int]:
    """
    t2s：ASR pre-roll（首段 nonidle 前环形缓冲）与 Duplug speak 后延迟收尾（ms）。

    Priority:
    1) Env: T2S_PRE_ROLL_MS / T2S_SPEAK_GRACE_MS
    2) config.json: debug.t2s_pre_roll_ms / debug.t2s_speak_grace_ms
    3) defaults: 1000 / 700
    """
    pre_roll = 1000
    speak_grace = 700
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            debug_cfg = data.get("debug", {}) or {}
            if "t2s_pre_roll_ms" in debug_cfg:
                pre_roll = int(debug_cfg["t2s_pre_roll_ms"])
            if "t2s_speak_grace_ms" in debug_cfg:
                speak_grace = int(debug_cfg["t2s_speak_grace_ms"])
    except Exception:
        pass
    env_pr = os.environ.get("T2S_PRE_ROLL_MS")
    if env_pr is not None and str(env_pr).strip():
        try:
            pre_roll = int(env_pr)
        except ValueError:
            pass
    env_sg = os.environ.get("T2S_SPEAK_GRACE_MS")
    if env_sg is not None and str(env_sg).strip():
        try:
            speak_grace = int(env_sg)
        except ValueError:
            pass
    return max(0, pre_roll), max(0, speak_grace)


def _load_t2s_vad_hysteresis_config() -> tuple[int, int]:
    """
    t2s: VAD end-point debounce/hysteresis tuning.

    Values:
    - min_speech_ms: minimum effective speech duration before allowing completion
    - min_turn_gap_ms: minimum interval between two finalized turns
    """
    min_speech_ms = 320
    min_turn_gap_ms = 400
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            debug_cfg = data.get("debug", {}) or {}
            if "t2s_min_speech_ms" in debug_cfg:
                min_speech_ms = int(debug_cfg["t2s_min_speech_ms"])
            if "t2s_min_turn_gap_ms" in debug_cfg:
                min_turn_gap_ms = int(debug_cfg["t2s_min_turn_gap_ms"])
    except Exception:
        pass

    env_ms = os.environ.get("T2S_MIN_SPEECH_MS")
    if env_ms is not None and str(env_ms).strip():
        try:
            min_speech_ms = int(env_ms)
        except ValueError:
            pass
    env_tg = os.environ.get("T2S_MIN_TURN_GAP_MS")
    if env_tg is not None and str(env_tg).strip():
        try:
            min_turn_gap_ms = int(env_tg)
        except ValueError:
            pass
    return (
        max(0, min_speech_ms),
        max(0, min_turn_gap_ms),
    )


def _load_t2s_tts_barge_suppress_ms() -> int:
    """
    t2sbg / t2sbg-omni：服务端发出首帧 TTS PCM 后的若干毫秒内，不向浏览器发 barge_in，
    也不把 VAD speech_start / Omni speech_started 当作「用户开说」（避免扬声器串音误打断）。

    - 0：关闭
    - 非 0：下限 100 ms（避免误配过小）；不设上限，由 config / 环境变量决定。

    优先级：环境变量 T2S_TTS_BARGE_SUPPRESS_MS → config.json debug.t2s_tts_barge_suppress_ms → 默认 400。
    """
    ms = 400
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            debug_cfg = data.get("debug", {}) or {}
            if "t2s_tts_barge_suppress_ms" in debug_cfg:
                ms = int(debug_cfg["t2s_tts_barge_suppress_ms"])
    except Exception:
        pass
    env_v = os.environ.get("T2S_TTS_BARGE_SUPPRESS_MS")
    if env_v is not None and str(env_v).strip():
        try:
            ms = int(env_v)
        except ValueError:
            pass
    if ms <= 0:
        return 0
    return max(100, ms)


DOUBAO_API_KEY = _load_doubao_api_key()
DEFAULT_DUPLEX_SERVER_VAD, DEFAULT_DUPLEX_END_SMOOTH_MS = _load_doubao_duplex_vad_config()
APP_ID, ACCESS_KEY = _load_doubao_credentials()
ASR_APP_ID, ASR_ACCESS_KEY = _load_asr_credentials(APP_ID, ACCESS_KEY)
T2S_DEBUG_AUDIO_DUMP, T2S_DEBUG_AUDIO_DIR = _load_t2s_debug_audio_config()
T2S_ASR_KEEPALIVE_IDLE_SEC, T2S_ASR_KEEPALIVE_TICK_SEC = _load_t2s_asr_keepalive_config()
T2S_PRE_ROLL_MS, T2S_SPEAK_GRACE_MS = _load_t2s_pre_roll_and_speak_grace_ms()
T2S_MIN_SPEECH_MS, T2S_MIN_TURN_GAP_MS = _load_t2s_vad_hysteresis_config()
T2S_TTS_BARGE_SUPPRESS_MS = _load_t2s_tts_barge_suppress_ms()

sessions: dict[str, dict] = {}

_monitor_clients_lock = asyncio.Lock()
_monitor_clients: list[WebSocket] = []
# 最近 N 次同步（任意用户），左侧最新；监控页晚打开也会收到重放。仅当前 worker 进程内可见。
MONITOR_SNAPSHOT_MAX = 5
_monitor_snapshot_ring: deque[dict[str, object]] = deque(maxlen=MONITOR_SNAPSHOT_MAX)


async def broadcast_t2sbg_monitor(payload: dict[str, object]) -> None:
    """Push a snapshot JSON to all connected /ws/t2sbg-monitor viewers."""
    payload = dict(payload)
    payload.setdefault("type", "monitor_snapshot")
    snap_store = dict(payload)
    async with _monitor_clients_lock:
        _monitor_snapshot_ring.appendleft(snap_store)
        targets = list(_monitor_clients)
    raw = json.dumps(payload, ensure_ascii=False)
    stale: list[WebSocket] = []
    for mws in targets:
        try:
            await mws.send_text(raw)
        except Exception:
            stale.append(mws)
    if stale:
        async with _monitor_clients_lock:
            for mws in stale:
                try:
                    _monitor_clients.remove(mws)
                except ValueError:
                    pass


def _startup_t2sbg_monitor_notice() -> None:
    """Log monitor URL; optionally open browser (T2SBG_MONITOR_AUTOSTART=1)."""
    port = (os.environ.get("PORT") or os.environ.get("UVICORN_PORT") or "7998").strip()
    base = (os.environ.get("T2SBG_PUBLIC_BASE") or f"http://127.0.0.1:{port}").rstrip("/")
    url = f"{base}/t2sbg-monitor"
    LOG.info(
        "T2SBG monitor page (sync target): %s — ring=%s (late viewers get replay; "
        "use uvicorn --workers 1 so all users share one ring)",
        url,
        MONITOR_SNAPSHOT_MAX,
    )
    if _parse_bool(os.environ.get("T2SBG_MONITOR_AUTOSTART"), default=False):
        import threading
        import webbrowser

        def _open_browser() -> None:
            time.sleep(0.7)
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("T2SBG_MONITOR_AUTOSTART: browser open failed: %s", exc)

        threading.Thread(target=_open_browser, daemon=True).start()


def _frontend_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "frontend"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ttfs_logger_dir() -> Path:
    """TTFS latency logger embed widget (iframe + shared ttfs-app.js)."""
    return _repo_root() / "ttfs_logger"


def _read_config_version(key: str, *, default: str = "0.0.0") -> str:
    """Read a version string from root config.json (safe for clients)."""
    cfg_path = _repo_root() / "config.json"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return default


def _read_t2sbg_voice_version_from_config() -> str:
    return _read_config_version("t2sbg_voice_version")


def _read_pipeline_voice_version_from_config() -> str:
    return _read_config_version("pipeline_voice_version")


def _read_omni_voice_version_from_config() -> str:
    return _read_config_version("omni_voice_version")


_T2SBG_TTFS_SCRIPT_MARKER = '<script src="/static/ttfs-app.js"></script>'


def _inline_ttfs_script_into_t2sbg_html(html: str) -> str:
    """Replace external ttfs script tag with inlined JS.

    Production reverse proxies often forward only /t2sbg-voice and /ws, not /static/*,
    which leaves TTFS uninitialized (no waveforms). Inlining avoids a second HTTP fetch.
    """
    if _T2SBG_TTFS_SCRIPT_MARKER not in html:
        return html
    js_path = _ttfs_logger_dir() / "ttfs-app.js"
    if not js_path.exists():
        LOG.warning("ttfs-app.js not found at %s; TTFS panel will not work without /static/", js_path)
        return html
    js_body = js_path.read_text(encoding="utf-8")
    js_body = js_body.replace("</script>", "<\\/script>")
    return html.replace(_T2SBG_TTFS_SCRIPT_MARKER, "<script>\n" + js_body + "\n</script>", 1)


def _serve_demo_unified_page() -> HTMLResponse:
    """Unified demo page: volcano T2SBG or Qwen Omni 3.5 on one screen."""
    path = _frontend_dir() / "demo_unified.html"
    if not path.exists():
        return HTMLResponse("<p>demo_unified.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("__T2SBG_VOICE_VERSION__", _read_t2sbg_voice_version_from_config())
    raw = raw.replace("__OMNI_VOICE_VERSION__", _read_omni_voice_version_from_config())
    raw = _inline_ttfs_script_into_t2sbg_html(raw)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@app.get("/demo")
async def demo_page():
    return _serve_demo_unified_page()


@app.get("/demo/asr")
async def demo_asr_page():
    path = _frontend_dir() / "demo_asr.html"
    if not path.exists():
        return HTMLResponse("<p>demo_asr.html not found</p>", status_code=404)
    return FileResponse(path)


@app.websocket("/ws/demo-asr")
async def websocket_demo_asr(websocket: WebSocket):
    await websocket.accept()
    last = demo_asr_last_event()
    if last is not None:
        try:
            await websocket.send_text(json.dumps(last, ensure_ascii=False))
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass
            return
    await register_demo_asr_viewer(websocket)
    try:
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                if 'Cannot call "receive" once a disconnect message has been received.' in str(e):
                    break
                raise
    finally:
        await unregister_demo_asr_viewer(websocket)


@app.get("/")
async def root():
    return FileResponse(_frontend_dir() / "index.html")


@app.get("/sip-call")
async def sip_call_page():
    """SIP 外呼联调页：测试手机号 + 催授权剧本槽位（外显号由服务端自动分配）。"""
    path = _frontend_dir() / "sip_call.html"
    if not path.exists():
        return HTMLResponse("<p>sip_call.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    try:
        from backend.outbound.dial import load_dial_settings

        cfg = load_dial_settings()
        boot = {
            "called_prefix": cfg.called_prefix,
            "ani": cfg.ani,
            "default_display": cfg.default_display,
            "display_numbers": list(cfg.display_numbers),
            "scenario": "urge_auth",
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("sip-call boot config failed: %s", exc)
        boot = {
            "called_prefix": "a1",
            "ani": "",
            "default_display": "",
            "display_numbers": [],
            "scenario": "urge_auth",
            "error": str(exc),
        }

    nums = [str(n).strip() for n in (boot.get("display_numbers") or []) if str(n).strip()]
    default = str(boot.get("default_display") or (nums[0] if nums else "")).strip()
    # 兼容旧模板占位；当前页面已不展示外显号选择
    raw = raw.replace("<!--__DISPLAY_OPTIONS__-->", "", 1)

    status = (
        f"外显号由服务端自动分配（默认 {default or '-'}，号池 {len(nums)} 个）。主叫 ANI={boot.get('ani') or '-'}"
        if nums
        else "未配置外显号池，请检查 config.json outbound.display_numbers / SIP_DEFAULT_DISPLAY"
    )
    raw = raw.replace("<!--__STATUS_BOOT__-->", status, 1)

    boot_json = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
    raw = raw.replace("<!--__SIP_CALL_BOOT_JSON__-->", boot_json, 1)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@app.get("/prod")
@app.get("/prod/")
@app.get("/tmp-prod")
@app.get("/tmp-prod/")
async def prod_page():
    """prod 演示页（/tmp-prod 为向前兼容别名）。同时挂有无无尾斜杠，避免反代下 307 丢 /s2s 前缀。"""
    from backend.prod.routes import render_prod_page

    return render_prod_page()


@app.get("/prod/single")
@app.get("/prod/single/")
@app.get("/tmp-prod/single")
@app.get("/tmp-prod/single/")
async def prod_single_page():
    """prod 单人外呼联调 + TTFS 延迟可视化（/tmp-prod 兼容）。"""
    from backend.prod.routes import render_prod_single_page

    return render_prod_single_page()


@app.get("/prod/voices")
@app.get("/prod/voices/")
@app.get("/tmp-prod/voices")
@app.get("/tmp-prod/voices/")
async def prod_voices_page():
    """prod PM 豆包音色选型（/tmp-prod 兼容）。"""
    from backend.prod.routes import render_prod_voices_page

    return render_prod_voices_page()


@app.get("/api/verification-case/default")
async def verification_case_default():
    """Default 背调核实案例 JSON（每人/每通可由网页覆盖）。"""
    from config.prompt_templates import (
        DEFAULT_VERIFICATION_CASE,
        build_verification_system_role,
        verification_case_to_json,
    )

    case = dict(DEFAULT_VERIFICATION_CASE)
    return JSONResponse(
        {
            "case": case,
            "case_json": verification_case_to_json(case),
            "system_role": build_verification_system_role(case),
        }
    )


@app.post("/api/verification-case/build")
async def verification_case_build(request: Request):
    """Build system_role from an external verification case body."""
    from config.prompt_templates import (
        build_verification_system_role,
        merge_verification_case,
        verification_case_to_json,
    )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    raw = payload.get("case") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict):
        # Allow posting the case object directly.
        raw = payload
    case = merge_verification_case(raw if isinstance(raw, dict) else None)
    return JSONResponse(
        {
            "case": case,
            "case_json": verification_case_to_json(case),
            "system_role": build_verification_system_role(case),
        }
    )


@app.get("/voice")
async def voice_page():
    path = _frontend_dir() / "voice.html"
    if not path.exists():
        return HTMLResponse("<p>voice.html not found</p>", status_code=404)
    return FileResponse(path)


@app.get("/t2s")
async def t2s_page():
    path = _frontend_dir() / "t2s.html"
    if not path.exists():
        return HTMLResponse("<p>t2s.html not found</p>", status_code=404)
    return FileResponse(path)


@app.get("/t2sbg-voice")
async def t2sbg_voice_page():
    """T2S 纯文本输入页：input_mod=text，支持文本直接发送或语音经豆包 ASR 识别后发送。"""
    path = _frontend_dir() / "t2sbg.html"
    if not path.exists():
        return HTMLResponse("<p>t2sbg.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("'__T2SBG_VOICE_VERSION__'", json.dumps(_read_t2sbg_voice_version_from_config()))
    raw = raw.replace("'__T2SBG_PAGE_LABEL__'", json.dumps("t2sbg-voice"))
    raw = _inline_ttfs_script_into_t2sbg_html(raw)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@app.get("/t2sbg-pipeline")
async def t2sbg_pipeline_page():
    """ASR→LLM→TTS pipeline demo (DashScope Responses LLM + qwen-audio TTS)."""
    path = _frontend_dir() / "t2sbg.html"
    if not path.exists():
        return HTMLResponse("<p>t2sbg.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("'__T2SBG_VOICE_VERSION__'", json.dumps(_read_pipeline_voice_version_from_config()))
    raw = raw.replace("'__T2SBG_PAGE_LABEL__'", json.dumps("t2sbg-pipeline"))
    raw = raw.replace("mode: 't2s'", "mode: 'pipeline'")
    raw = raw.replace(
        "芝麻背调证明人访谈（T2S 纯文本 → 语音）",
        "芝麻背调证明人访谈（ASR→LLM→TTS pipeline）",
    )
    raw = _inline_ttfs_script_into_t2sbg_html(raw)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@app.get("/t2sbg-omni")
async def t2sbg_omni_page():
    """Omni 直连演示页（Qwen Omni；默认 Smart Turn v3 本地判停 + manual commit）。"""
    path = _frontend_dir() / "t2sbg_omni.html"
    if not path.exists():
        return HTMLResponse("<p>t2sbg_omni.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("'__OMNI_VOICE_VERSION__'", json.dumps(_read_omni_voice_version_from_config()))
    raw = _inline_ttfs_script_into_t2sbg_html(raw)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@app.get("/t2sbg-monitor")
async def t2sbg_monitor_page():
    """被动接收 t2sbg 访谈页「同步到监控页」快照（录音自后端内存 + 正文来自浏览器）。"""
    path = _frontend_dir() / "t2sbg_monitor.html"
    if not path.exists():
        return HTMLResponse("<p>t2sbg_monitor.html not found</p>", status_code=404)
    return FileResponse(path)


@app.get("/embed.html")
async def ttfs_embed_page():
    """TTFS latency widget — same-origin iframe for t2sbg-voice and other demos."""
    path = _ttfs_logger_dir() / "embed.html"
    if not path.exists():
        return HTMLResponse("<p>embed.html not found</p>", status_code=404)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/static/ttfs-app.js")
async def ttfs_app_js():
    """Shared TTFS script referenced by embed.html."""
    path = _ttfs_logger_dir() / "ttfs-app.js"
    if not path.exists():
        return HTMLResponse("// ttfs-app.js not found\n", status_code=404, media_type="application/javascript")
    return FileResponse(path, media_type="application/javascript; charset=utf-8")


@app.websocket("/ws/t2sbg-monitor")
async def websocket_t2sbg_monitor(websocket: WebSocket):
    await websocket.accept()
    # Oldest → newest so monitor UI (prepend per message) ends with newest on top.
    async with _monitor_clients_lock:
        history = list(reversed(_monitor_snapshot_ring))
    for snap in history:
        try:
            await websocket.send_text(json.dumps(snap, ensure_ascii=False))
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass
            return
    async with _monitor_clients_lock:
        _monitor_clients.append(websocket)
    try:
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                if 'Cannot call "receive" once a disconnect message has been received.' in str(e):
                    break
                raise
    finally:
        async with _monitor_clients_lock:
            try:
                _monitor_clients.remove(websocket)
            except ValueError:
                pass


def _create_omni_bridge(websocket: WebSocket, initial_cfg: dict) -> OmniRealtimeBridge | SelfDeployedOmniBridge:
    """Pick Qwen DashScope Omni or self-deployed Omni based on client config."""
    backend = (initial_cfg.get("omni_backend") or "qwen").strip().lower()
    if backend in {"self", "self_deployed", "custom"}:
        return SelfDeployedOmniBridge(websocket)
    return OmniRealtimeBridge(websocket)


@app.websocket("/ws-omni")
async def websocket_omni_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        # Keep first client frame compatible with existing frontend config style.
        initial_cfg: dict = {}
        try:
            raw = await websocket.receive_text()
            initial_cfg = json.loads(raw) if raw else {}
            if not isinstance(initial_cfg, dict):
                initial_cfg = {}
        except Exception:
            initial_cfg = {}

        bridge = _create_omni_bridge(websocket, initial_cfg)
        await bridge.start(initial_cfg)

        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                # Same guard pattern as /ws endpoint: receive() after disconnect.
                if 'Cannot call "receive" once a disconnect message has been received.' in str(e):
                    break
                raise
            if "bytes" in msg and msg["bytes"]:
                await bridge.handle_client_audio(msg["bytes"])
            elif "text" in msg and msg["text"]:
                try:
                    obj = json.loads(msg["text"])
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "monitor_publish":
                    ft_raw = obj.get("full_turn_wavs_base64")
                    ft_list = [str(x) for x in ft_raw] if isinstance(ft_raw, list) else []
                    snap = bridge.build_monitor_snapshot(
                        transcript_html=str(obj.get("transcript_html") or ""),
                        log_html=str(obj.get("log_html") or ""),
                        full_merged_wav_b64=str(obj.get("full_merged_wav_base64") or ""),
                        full_turn_wavs_b64=[x for x in ft_list if x],
                    )
                    await broadcast_t2sbg_monitor(snap)
                    async with _monitor_clients_lock:
                        n_sub = len(_monitor_clients)
                    await bridge._send_client_json(
                        {"type": "monitor_publish_ok", "monitor_subscribers": n_sub}
                    )
                else:
                    await bridge.handle_client_text(msg["text"])
    except WebSocketDisconnect:
        LOG.info("Omni websocket disconnected")
    except Exception as e:
        LOG.exception("Omni WS handler error: %s", e)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        await bridge.close()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn_id = str(uuid.uuid4())
    mode = "e2e"
    prompt_manager = PromptManager()
    latency_logger = LatencyLogger()
    followup_client = FollowupQuestionClient()
    # 队列里放结构化信息，方便在日志中关联“哪一句回答触发了哪一个追问”
    pending_followups: list[dict] = []
    external_followup_enabled = True
    duplex_server_vad = DEFAULT_DUPLEX_SERVER_VAD
    duplex_end_smooth_ms = DEFAULT_DUPLEX_END_SMOOTH_MS
    from backend.duplex_client import DEFAULT_SPEAKER as _WS_DEFAULT_SPEAKER

    session_voice = _WS_DEFAULT_SPEAKER
    client: DuplexVoiceClient | None = None
    pipeline_orch = None
    # 标记当前这一轮模型回复（文本/音频）是否还在进行中，主要用于 one-shot 501 防止抢占打断
    reply_in_progress = False
    barge_in_flag = False
    dialog_id_latest = ""
    restart_lock = asyncio.Lock()
    seen_llm_start_reply_ids: set[str] = set()
    last_question_id: str = ""
    last_reply_id: str = ""
    # Rolling dialogue history for follow-up model (simple text lines).
    dialogue_history: list[str] = []
    # Demo control: auto-send a 501 built from ASR text when ASREnded(459) arrives。
    auto_501_from_asr_enabled = True
    # One-shot: send a configured 501 at the next ASREnded, then auto-disable.
    next_turn_501_enabled = False
    next_turn_501_content = ""
    # 限制“同一轮用户发言”里只触发一次自动追问：
    # - 发送自动 501 后置为 True
    # - 在下一次最终 ASR（用户新一轮发言）到来时清零
    auto_followup_waiting_user = False
    # t2s 模式下：缓存一段原始 PCM（非流式时使用），或流式 ASR 会话。
    t2s_pcm_buffer = bytearray()
    t2s_asr_streaming: VolcAsrStreamingSession | None = None
    t2s_asr_stream_buffer = bytearray()  # 流式时按 200ms 分包缓冲
    t2s_asr_capture_buffer = bytearray()  # 调试：按“实际送入 ASR 的顺序”累计整轮音频
    t2s_vad_turn_buffer = bytearray()  # 调试：按“实际送入 VAD 的顺序”累计当前轮音频
    t2s_vad_merged_buffer = bytearray()  # 调试：会话累计 VAD 音频（内存）
    t2s_asr_merged_buffer = bytearray()  # 调试：会话累计 ASR 音频（内存）
    t2s_vad_turn_pcm_history: list[bytes] = []  # 按轮快照，供监控页未合并列表
    t2s_asr_turn_pcm_history: list[bytes] = []
    t2s_vad_segments = 0
    t2s_asr_segments = 0
    asr_client: VolcAsrClient | None = None
    # t2s + pluggable VAD (Smart Turn v3 / Duplug) for barge-in & EoU
    t2s_vad = None
    t2s_voice_active = False
    t2s_speech_started = False
    t2s_eou_fired = False
    t2s_turn_id = 0
    t2s_complete_task: asyncio.Task | None = None
    # VAD-driven turn logic (t2sbg):
    # - barge-in on first nonidle
    # - open ASR gate only after nonidle
    # - trigger completion after 4 consecutive idle states (post-speech)
    t2s_vad_idle_streak = 0
    t2s_asr_gate_open = False
    # Pre-roll before first nonidle: capture audio ahead of ASR gate open (semantic VAD can lag).
    pre_roll_ms = T2S_PRE_ROLL_MS
    pre_roll_chunks = max(1, pre_roll_ms // 20)
    t2s_pre_roll_chunks: deque[bytes] = deque(maxlen=pre_roll_chunks)
    # 默认关闭 idle4 自动结束，避免静默抖动导致过早截断。
    t2s_idle4_complete_enabled = (
        os.environ.get("T2S_IDLE4_COMPLETE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    )
    # Duplug `speak` 后延迟再结束 ASR；期间若再次出现 `nonidle` 则取消并继续录制。
    t2s_speak_grace_ms = T2S_SPEAK_GRACE_MS
    t2s_speak_grace_task: asyncio.Task | None = None
    # 防抖参数：抑制句内停顿误判 turn-end。
    t2s_min_speech_ms = T2S_MIN_SPEECH_MS
    t2s_min_turn_gap_ms = T2S_MIN_TURN_GAP_MS
    t2s_speech_started_ts: float = 0.0
    t2s_last_nonidle_ts: float = 0.0
    t2s_last_complete_ts: float = 0.0
    # 本轮 TTS：首帧 PCM 发给浏览器时的 monotonic 时间（0 表示尚未发过帧）
    t2s_tts_first_audio_mono: float = 0.0
    # 调试：可选音频落盘（默认关闭）
    t2s_debug_audio_dump_enabled = T2S_DEBUG_AUDIO_DUMP
    t2s_debug_audio_dir = Path(T2S_DEBUG_AUDIO_DIR)
    t2s_debug_vad_file = None
    t2s_debug_vad_pcm_path = ""
    t2s_debug_vad_mp4_path = ""
    t2s_debug_asr_dumped = False
    # 上轮追问模型超时未完成时，将追问句暂存，合并到「下一轮」ASR 对应的 501 末尾。
    t2s_pending_followup_question: str = ""
    # 流式 ASR 长时间无音频会 45000081 等包超时：在「无真实人声」时周期性送静音保持连接。
    t2s_asr_last_real_send_monotonic: float = 0.0
    t2s_asr_last_keepalive_monotonic: float = 0.0
    t2s_asr_keepalive_task: asyncio.Task | None = None
    _t2s_asr_silence_chunk: bytes = bytes(ASR_CHUNK_BYTES)
    t2s_input_sample_rate = VAD_ASR_TARGET_SAMPLE_RATE
    t2s_hush = None

    def _denoise_t2s_uplink_pcm(pcm_s16le: bytes) -> bytes:
        if t2s_hush is None:
            return pcm_s16le
        return t2s_hush.process_pcm_s16le(pcm_s16le)

    def _set_t2s_input_sample_rate(raw_value: object) -> int:
        """Update browser upstream sample rate, defaulting to 16k on invalid input."""
        nonlocal t2s_input_sample_rate
        try:
            parsed = int(raw_value)
            if parsed <= 0:
                raise ValueError("sample_rate must be > 0")
            t2s_input_sample_rate = parsed
        except Exception:
            t2s_input_sample_rate = VAD_ASR_TARGET_SAMPLE_RATE
        return t2s_input_sample_rate

    def _resample_linear(audio_f32: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
        """Fallback resampler used when python-soxr is unavailable."""
        if audio_f32.size == 0 or in_rate <= 0 or out_rate <= 0 or in_rate == out_rate:
            return audio_f32
        out_len = max(1, int(round(audio_f32.size * (out_rate / in_rate))))
        in_idx = np.arange(audio_f32.size, dtype=np.float32)
        out_idx = np.linspace(0, max(0, audio_f32.size - 1), out_len, dtype=np.float32)
        return np.interp(out_idx, in_idx, audio_f32).astype(np.float32)

    def _normalize_input_audio_to_16k(raw_pcm_s16le: bytes) -> bytes:
        """
        Browser upstream is PCM s16le mono. Convert to float32 first, then:
        - if input SR != 16k, resample to 16k for VAD/ASR
        - else pass through without resampling
        """
        in_bytes = raw_pcm_s16le
        if len(in_bytes) % 2 != 0:
            in_bytes = in_bytes[:-1]
        if not in_bytes:
            return b""

        i16 = np.frombuffer(in_bytes, dtype="<i2")
        if i16.size == 0:
            return b""
        audio = i16.astype(np.float32)
        audio /= np.where(audio < 0, 32768.0, 32767.0)

        current_sr = int(t2s_input_sample_rate or VAD_ASR_TARGET_SAMPLE_RATE)
        if current_sr != VAD_ASR_TARGET_SAMPLE_RATE:
            if soxr is not None:
                audio = soxr.resample(audio, current_sr, VAD_ASR_TARGET_SAMPLE_RATE).astype(np.float32)
            else:
                audio = _resample_linear(audio, current_sr, VAD_ASR_TARGET_SAMPLE_RATE)

        out = np.where(audio < 0, audio * 32768.0, audio * 32767.0)
        out_i16 = np.clip(np.round(out), -32768, 32767).astype("<i2")
        return out_i16.tobytes()

    def _cancel_t2s_speak_grace() -> None:
        nonlocal t2s_speak_grace_task
        if t2s_speak_grace_task and not t2s_speak_grace_task.done():
            t2s_speak_grace_task.cancel()
        t2s_speak_grace_task = None

    def _schedule_t2s_speak_grace_complete(*, source: str) -> None:
        """On semantic turn-end (`speak`), wait grace_ms then finalize ASR; cancel on next speech."""
        nonlocal t2s_speak_grace_task
        _cancel_t2s_speak_grace()
        if t2s_speak_grace_ms <= 0:
            asyncio.create_task(_t2s_trigger_complete(source=source))
            return

        async def _grace_wait() -> None:
            me = asyncio.current_task()
            try:
                await asyncio.sleep(t2s_speak_grace_ms / 1000.0)
                await _t2s_trigger_complete(source=source)
            except asyncio.CancelledError:
                raise
            finally:
                nonlocal t2s_speak_grace_task
                if me is not None and t2s_speak_grace_task is me:
                    t2s_speak_grace_task = None

        t2s_speak_grace_task = asyncio.create_task(_grace_wait())

    def _cancel_t2s_asr_keepalive() -> None:
        nonlocal t2s_asr_keepalive_task
        if t2s_asr_keepalive_task and not t2s_asr_keepalive_task.done():
            t2s_asr_keepalive_task.cancel()
        t2s_asr_keepalive_task = None

    def _touch_t2s_asr_real_send() -> None:
        nonlocal t2s_asr_last_real_send_monotonic
        t2s_asr_last_real_send_monotonic = time.monotonic()

    async def _t2s_asr_keepalive_loop(asr_session) -> None:
        nonlocal t2s_asr_last_keepalive_monotonic
        idle_s = T2S_ASR_KEEPALIVE_IDLE_SEC
        tick_s = T2S_ASR_KEEPALIVE_TICK_SEC
        silence = _t2s_asr_silence_chunk
        try:
            while (
                mode == "t2s"
                and t2s_asr_streaming is asr_session
                and not t2s_eou_fired
            ):
                await asyncio.sleep(tick_s)
                if t2s_asr_streaming is not asr_session or t2s_eou_fired:
                    break
                now = time.monotonic()
                last_real = t2s_asr_last_real_send_monotonic
                real_idle = (last_real == 0.0) or (now - last_real >= idle_s)
                if not real_idle:
                    continue
                last_k = t2s_asr_last_keepalive_monotonic
                if last_k != 0.0 and (now - last_k) < idle_s:
                    continue
                try:
                    if hasattr(asr_session, "send_keepalive"):
                        await asr_session.send_keepalive()
                    else:
                        await asr_session.send_audio(silence, is_last=False)
                    t2s_asr_last_keepalive_monotonic = now
                except Exception as exc:
                    LOG.debug("ASR keepalive send failed: %s", exc)
                    break
        except asyncio.CancelledError:
            pass

    def _transcode_pcm16k_s16le_to_mp4(pcm_path: Path, mp4_path: Path) -> bool:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return False
        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-i",
            str(pcm_path),
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            str(mp4_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            return proc.returncode == 0 and mp4_path.exists()
        except Exception:
            return False

    def _open_t2s_debug_files_for_turn() -> None:
        nonlocal t2s_debug_vad_file, t2s_debug_vad_pcm_path, t2s_debug_vad_mp4_path, t2s_debug_asr_dumped
        if not t2s_debug_audio_dump_enabled:
            return
        try:
            t2s_debug_audio_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            base = f"{ts}-conn-{conn_id[:8]}-turn-{t2s_turn_id}"
            t2s_debug_vad_pcm_path = str(t2s_debug_audio_dir / f"{base}.vad-feed.pcm")
            t2s_debug_vad_mp4_path = str(t2s_debug_audio_dir / f"{base}.vad-feed.mp4")
            t2s_debug_vad_file = open(t2s_debug_vad_pcm_path, "ab")
            t2s_debug_asr_dumped = False
            send_json(
                {
                    "type": "debug_audio",
                    "enabled": True,
                    "event": "turn_files_opened",
                    "vad_pcm_path": t2s_debug_vad_pcm_path,
                    "vad_mp4_path": t2s_debug_vad_mp4_path,
                }
            )
        except Exception as exc:
            LOG.warning("open debug audio file failed: %s", exc)
            t2s_debug_vad_file = None
            t2s_debug_vad_pcm_path = ""
            t2s_debug_vad_mp4_path = ""

    def _close_t2s_debug_files() -> None:
        nonlocal t2s_debug_vad_file
        if t2s_debug_vad_file is not None:
            try:
                t2s_debug_vad_file.close()
            except Exception:
                pass
            t2s_debug_vad_file = None
        if t2s_debug_vad_pcm_path and t2s_debug_vad_mp4_path:
            pcm_path = Path(t2s_debug_vad_pcm_path)
            mp4_path = Path(t2s_debug_vad_mp4_path)
            if pcm_path.exists():
                ok = _transcode_pcm16k_s16le_to_mp4(pcm_path, mp4_path)
                if ok:
                    try:
                        pcm_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    send_json(
                        {
                            "type": "debug_audio",
                            "enabled": True,
                            "event": "vad_capture_dumped",
                            "format": "mp4",
                            "vad_path": str(mp4_path),
                        }
                    )
                else:
                    send_json(
                        {
                            "type": "debug_audio",
                            "enabled": True,
                            "event": "vad_capture_dumped",
                            "format": "pcm_fallback",
                            "vad_path": str(pcm_path),
                        }
                    )

    def _dump_t2s_asr_capture_once(source: str) -> None:
        nonlocal t2s_debug_asr_dumped
        if not t2s_debug_audio_dump_enabled or t2s_debug_asr_dumped:
            return
        if not t2s_asr_capture_buffer:
            return
        try:
            t2s_debug_audio_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            pcm_path = t2s_debug_audio_dir / (
                f"{ts}-conn-{conn_id[:8]}-turn-{t2s_turn_id}.asr-input-{source}.pcm"
            )
            mp4_path = t2s_debug_audio_dir / (
                f"{ts}-conn-{conn_id[:8]}-turn-{t2s_turn_id}.asr-input-{source}.mp4"
            )
            pcm_path.write_bytes(bytes(t2s_asr_capture_buffer))
            ok = _transcode_pcm16k_s16le_to_mp4(pcm_path, mp4_path)
            out_path = mp4_path if ok else pcm_path
            out_format = "mp4" if ok else "pcm_fallback"
            if ok:
                try:
                    pcm_path.unlink(missing_ok=True)
                except Exception:
                    pass
            t2s_debug_asr_dumped = True
            send_json(
                {
                    "type": "debug_audio",
                    "enabled": True,
                    "event": "asr_capture_dumped",
                    "source": source,
                    "format": out_format,
                    "asr_path": str(out_path),
                    "bytes": len(t2s_asr_capture_buffer),
                }
            )
        except Exception as exc:
            LOG.warning("dump ASR debug audio failed: %s", exc)

    async def _send_text_safe(text: str) -> None:
        try:
            await websocket.send_text(text)
        except Exception:
            pass

    async def _send_bytes_safe(data: bytes) -> None:
        try:
            await websocket.send_bytes(data)
        except Exception:
            pass

    def send_json(obj: dict) -> asyncio.Task | None:
        if isinstance(obj, dict) and obj.get("type") in DEMO_ASR_EVENT_TYPES:
            schedule_demo_asr_broadcast(obj)
        try:
            return asyncio.create_task(_send_text_safe(json.dumps(obj, ensure_ascii=False)))
        except Exception:
            return None

    def push_server_log(level: str, message: str, **extra: object) -> None:
        """Push a line to t2sbg debug panel (in-memory, not disk)."""
        payload: dict = {"type": "server_log", "level": level, "message": message}
        payload.update({k: v for k, v in extra.items() if v is not None})
        send_json(payload)

    def _t2s_tts_barge_suppress_active() -> bool:
        """首帧 TTS 发出后的保护窗内为 True：不触发 barge_in / 不把 VAD speech_start 当用户开说。"""
        if mode == "pipeline":
            return bool(pipeline_orch and pipeline_orch.tts_barge_suppress_active())
        if mode != "t2s" or T2S_TTS_BARGE_SUPPRESS_MS <= 0:
            return False
        if t2s_tts_first_audio_mono <= 0:
            return False
        return (time.monotonic() - t2s_tts_first_audio_mono) * 1000.0 < float(T2S_TTS_BARGE_SUPPRESS_MS)

    async def _forward_split_asr_pcm(pcm: bytes) -> None:
        if mode == "pipeline":
            if pipeline_orch:
                await pipeline_orch.feed_pcm(pcm)
            return
        if t2s_asr_streaming:
            t2s_asr_stream_buffer.extend(pcm)
            t2s_asr_capture_buffer.extend(pcm)
            while len(t2s_asr_stream_buffer) >= ASR_CHUNK_BYTES:
                to_send = bytes(t2s_asr_stream_buffer[:ASR_CHUNK_BYTES])
                del t2s_asr_stream_buffer[:ASR_CHUNK_BYTES]
                await t2s_asr_streaming.send_audio(to_send, is_last=False)
                _touch_t2s_asr_real_send()
        else:
            t2s_pcm_buffer.extend(pcm)
            t2s_asr_capture_buffer.extend(pcm)

    async def _apply_t2s_vad_event(ev: VadEvent) -> bool:
        """
        Map VAD module events to t2s turn logic.
        Returns True if the current PCM chunk was already forwarded via pre-roll flush.
        """
        nonlocal barge_in_flag, t2s_speech_started, t2s_speech_started_ts, t2s_last_nonidle_ts
        nonlocal t2s_asr_gate_open, t2s_vad_idle_streak, t2s_asr_stream_buffer, t2s_pcm_buffer
        nonlocal t2s_asr_capture_buffer, t2s_pre_roll_chunks

        payload: dict[str, object] = {
            "type": "vad_state",
            "provider": ev.provider,
            "state": ev.state,
        }
        if ev.meta:
            payload.update(ev.meta)
        send_json(payload)

        if ev.kind == "speech_start":
            _cancel_t2s_speak_grace()
            t2s_last_nonidle_ts = time.monotonic()
            if not t2s_speech_started:
                if _t2s_tts_barge_suppress_active():
                    LOG.info(
                        "t2s VAD speech_start ignored (TTS lead-in %sms)",
                        T2S_TTS_BARGE_SUPPRESS_MS,
                    )
                    push_server_log(
                        "info",
                        "VAD speech_start ignored (TTS lead-in window)",
                        ms=T2S_TTS_BARGE_SUPPRESS_MS,
                    )
                    return False
                t2s_speech_started = True
                t2s_speech_started_ts = t2s_last_nonidle_ts
                if mode == "pipeline" and pipeline_orch:
                    await pipeline_orch.on_speech_start(source=str(ev.provider))
                else:
                    barge_in_flag = True
                    send_json(
                        {
                            "type": "barge_in",
                            "source": ev.provider,
                            "reason": "user_speech",
                            "reason_label": "用户打断（检测到说话）",
                        }
                    )
                t2s_asr_gate_open = True
                t2s_vad_idle_streak = 0
                if t2s_pre_roll_chunks:
                    if mode == "pipeline" or t2s_asr_streaming:
                        for pr in list(t2s_pre_roll_chunks):
                            await _forward_split_asr_pcm(pr)
                    else:
                        for pr in list(t2s_pre_roll_chunks):
                            t2s_pcm_buffer.extend(pr)
                            t2s_asr_capture_buffer.extend(pr)
                    t2s_pre_roll_chunks.clear()
                    return True
            else:
                t2s_vad_idle_streak = 0
        elif ev.kind == "speech_continue":
            _cancel_t2s_speak_grace()
            t2s_vad_idle_streak = 0
        elif ev.kind == "turn_end_candidate":
            _schedule_t2s_speak_grace_complete(source=ev.provider)
        elif ev.kind == "silence":
            if t2s_speech_started and t2s_idle4_complete_enabled:
                t2s_vad_idle_streak += 1
                if t2s_vad_idle_streak >= 4:
                    asyncio.create_task(_t2s_trigger_complete(source="idle4"))
        return False

    def _t2s_append_followup_to_asr_text(asr_text: str, follow_q: str) -> str:
        """单条 501：在 ASR 文本末尾追加追问系统指令（不另发第二条 501）。"""
        fq = (follow_q or "").strip()
        if not fq:
            return (asr_text or "").strip()
        base = (asr_text or "").strip()
        return (
            f"{base}\n\n"
            "【系统指令】接上文证明人回答，请用自然口语补充提出下面这个追问，只问这一句，不要复述全文，不要自己先作答。\n"
            f"追问：{fq}"
        )

    def _pcm16le_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
        data_len = len(pcm_bytes)
        byte_rate = sample_rate * channels * 2
        block_align = channels * 2
        riff_size = 36 + data_len
        header = (
            b"RIFF"
            + riff_size.to_bytes(4, "little")
            + b"WAVE"
            + b"fmt "
            + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + channels.to_bytes(2, "little")
            + sample_rate.to_bytes(4, "little")
            + byte_rate.to_bytes(4, "little")
            + block_align.to_bytes(2, "little")
            + (16).to_bytes(2, "little")
            + b"data"
            + data_len.to_bytes(4, "little")
        )
        return header + pcm_bytes

    def _send_t2s_debug_preview(
        *,
        vad_last_turn_wav_b64: str = "",
        asr_last_turn_wav_b64: str = "",
    ) -> None:
        vad_pcm = bytes(t2s_vad_merged_buffer)
        asr_pcm = bytes(t2s_asr_merged_buffer)
        vad_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(vad_pcm)).decode("ascii") if vad_pcm else ""
        asr_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(asr_pcm)).decode("ascii") if asr_pcm else ""
        payload: dict[str, object] = {
            "type": "debug_audio_preview",
            "vad_wav_base64": vad_wav_b64,
            "asr_wav_base64": asr_wav_b64,
            "vad_segments": t2s_vad_segments,
            "asr_segments": t2s_asr_segments,
            "vad_duration_sec": round(len(vad_pcm) / 2 / 16000, 3),
            "asr_duration_sec": round(len(asr_pcm) / 2 / 16000, 3),
        }
        if vad_last_turn_wav_b64:
            payload["vad_last_turn_wav_base64"] = vad_last_turn_wav_b64
        if asr_last_turn_wav_b64:
            payload["asr_last_turn_wav_base64"] = asr_last_turn_wav_b64
        send_json(payload)

    def _build_monitor_snapshot(
        *,
        transcript_html: str,
        log_html: str,
        full_merged_wav_b64: str,
        full_turn_wavs_b64: list[str],
    ) -> dict[str, object]:
        vad_pcm = bytes(t2s_vad_merged_buffer)
        asr_pcm = bytes(t2s_asr_merged_buffer)
        vad_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(vad_pcm)).decode("ascii") if vad_pcm else ""
        asr_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(asr_pcm)).decode("ascii") if asr_pcm else ""
        vad_turn_b64 = [
            base64.b64encode(_pcm16le_to_wav_bytes(seg)).decode("ascii") for seg in t2s_vad_turn_pcm_history if seg
        ]
        asr_turn_b64 = [
            base64.b64encode(_pcm16le_to_wav_bytes(seg)).decode("ascii") for seg in t2s_asr_turn_pcm_history if seg
        ]
        published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "type": "monitor_snapshot",
            "vad_wav_base64": vad_wav_b64,
            "asr_wav_base64": asr_wav_b64,
            "vad_turn_wavs_base64": vad_turn_b64,
            "asr_turn_wavs_base64": asr_turn_b64,
            "vad_segments": t2s_vad_segments,
            "asr_segments": t2s_asr_segments,
            "vad_duration_sec": round(len(vad_pcm) / 2 / 16000, 3),
            "asr_duration_sec": round(len(asr_pcm) / 2 / 16000, 3),
            "transcript_html": transcript_html,
            "log_html": log_html,
            "full_merged_wav_base64": full_merged_wav_b64,
            "full_turn_wavs_base64": full_turn_wavs_b64,
            "published_at": published_at,
            "source_conn_id": conn_id,
        }

    def on_asr(text: str, is_interim: bool):
        nonlocal auto_followup_waiting_user
        send_json({"type": "asr", "text": text, "is_interim": is_interim})
        if t2s_vad is not None and hasattr(t2s_vad, "update_user_transcript"):
            t2s_vad.update_user_transcript(text)
        if not is_interim:
            # 用户完成一轮清晰回答，允许下一次自动追问重新触发。
            auto_followup_waiting_user = False
            latency_logger.asr_complete()

    def _append_history(role: str, text: str) -> None:
        if not text:
            return
        entry = f"{role}: {text}"
        dialogue_history.append(entry)
        # Keep only the last 40 lines to bound prompt size.
        if len(dialogue_history) > 40:
            del dialogue_history[: len(dialogue_history) - 40]

    def _wrap_followup_for_chat_model(raw_question: str) -> str:
        """Wrap external follow-up so chat model继续以语音访谈的口吻追问候选人。"""
        raw_question = (raw_question or "").strip()
        return (
            "你现在正在和候选人做实时语音访谈，请根据下面的“追问意图”，"
            "用自然口语继续围绕候选人刚才的回答追问一个问题。\n"
            "严格要求：\n"
            "- 只输出一个简短的口语化问句，好像你正在和候选人对话\n"
            "- 必须紧扣当前话题，不能随意切换到全新话题\n"
            "- 可以在措辞上略作调整，使其更自然，但不要改变核心含义\n"
            "- 不要解释、不分析、不总结，不要自己回答问题\n"
            "- 不要输出多条问题或列表，只保留最终要说给候选人的一句话\n\n"
            f"追问意图：{raw_question}"
        )

    async def _generate_followup_and_enqueue(final_text: str) -> None:
        """Call external follow-up model and push result into local queue."""
        if not final_text:
            return
        if not followup_client.enabled:
            return

        try:
            history_str = "\n".join(dialogue_history[-20:])
            question = await followup_client.generate_followup(
                final_text,
                stage=prompt_manager.stage,
                dialogue_history=history_str or None,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("followup model failed: %s", exc)
            send_json({"type": "error", "message": f"followup model error: {exc}"})
            return

        question = (question or "").strip()
        if not question:
            # 模型显式判定“本轮不需要追问”——在前端 log 中也体现出来，方便事后复盘。
            send_json(
                {
                    "type": "followup_skipped",
                    "reason": "model_decision_no_followup",
                    "source_asr": final_text,
                    "stage": prompt_manager.stage,
                }
            )
            return

        info = {
            "question": question,
            "source_asr": final_text,
            "stage": prompt_manager.stage,
        }
        pending_followups.append(info)
        send_json(
            {
                "type": "followup_generated",
                "question": question,
                "source_asr": final_text,
                "stage": prompt_manager.stage,
            }
        )

    def on_asr_ended(final_text: str):
        """Semi mode: when user stops speaking, inject ASR text and send ChatTextQuery.

        Also supports in-session steering via ConversationCreate (510) for testing.
        """
        nonlocal barge_in_flag
        barge_in_flag = False
        # For latency measurement: ASREnded ~= user_speech_end in server_vad mode
        latency_logger.user_speech_end()
        if not client:
            return

        # For debugging/inspection: surface ASREnded(459) to frontend log.
        send_json({"type": "asr_ended", "final_text": final_text})

        # Update dialogue history with the final ASR text as a user/证明人发言（仅 s2s / semi 使用）。
        if final_text and mode != "t2s":
            _append_history("证明人", final_text)

        # t2s 模式：使用外部 ASR 将缓存的 PCM 转文字，然后以文本 query 发送给豆包。
        if mode == "t2s":
            nonlocal asr_client
            if not client:
                return
            if not t2s_pcm_buffer:
                return
            pcm_bytes = bytes(t2s_pcm_buffer)
            t2s_pcm_buffer.clear()

            async def _run_external_asr_and_query() -> None:
                # 懒加载外部 ASR 客户端，使用与 Doubao 相同的 app_id/access_key。
                nonlocal asr_client
                if asr_client is None:
                    rid = os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
                    asr_client = VolcAsrClient(ASR_APP_ID, ASR_ACCESS_KEY, resource_id=rid)
                text = await asr_client.transcribe_pcm16le_16k(pcm_bytes, language="zh-CN")
                text = (text or "").strip()
                if not text:
                    send_json({"type": "error", "message": "external ASR produced empty text in t2s mode"})
                    return
                send_json({"type": "local_asr_text", "text": text})
                await client.send_text_query(text)
                send_json({"type": "chat_text_query_sent", "reason": "t2s_external_asr", "trigger": "asr_ended"})

            asyncio.create_task(_run_external_asr_and_query())
            return

        if auto_501_from_asr_enabled and followup_client.enabled and external_followup_enabled:
            # 基于本轮 ASR 文本，异步生成“下一条追问”并入队，真正发送 501 放在 ChatEnded。
            if final_text:
                LOG.info(
                    "Auto-501 enqueue followup from ASR; text_len=%d", len(final_text or "")
                )
                send_json(
                    {
                        "type": "auto_501_decision",
                        "phase": "asr_ended",
                        "action": "enqueue_followup",
                        "pending_len_before": len(pending_followups),
                    }
                )
                asyncio.create_task(_generate_followup_and_enqueue(final_text))

        # (501) One-shot manual override: send a pre-configured 501 on the next ASREnded.
        # This is used to simulate “another model produced the next question”.
        nonlocal next_turn_501_enabled, next_turn_501_content
        # 同样受 reply_in_progress 保护：若模型还在回复，则等下一次 ASREnded 再发送。
        if (not reply_in_progress) and next_turn_501_enabled and next_turn_501_content.strip():
            content = next_turn_501_content.strip()
            next_turn_501_enabled = False
            asyncio.create_task(client.send_text_query(content))
            send_json({"type": "next_turn_501_sent"})

    def on_tts_audio(data: bytes):
        nonlocal t2s_tts_first_audio_mono
        if mode == "t2s" and data and t2s_tts_first_audio_mono <= 0:
            t2s_tts_first_audio_mono = time.monotonic()
        if barge_in_flag:
            return
        try:
            asyncio.create_task(_send_bytes_safe(data))
        except Exception:
            pass

    def on_tts_start(payload: dict):
        nonlocal barge_in_flag
        nonlocal last_question_id, last_reply_id, reply_in_progress
        nonlocal t2s_tts_first_audio_mono
        t2s_tts_first_audio_mono = 0.0
        barge_in_flag = False
        reply_in_progress = True
        last_question_id = payload.get("question_id", "") or last_question_id
        last_reply_id = payload.get("reply_id", "") or last_reply_id
        latency_logger.tts_start(
            payload.get("question_id", ""),
            payload.get("reply_id", ""),
        )
        total_ms = latency_logger.get_last_total_latency_ms()
        if total_ms is not None:
            send_json({"type": "latency", "tts_start_minus_user_end_ms": round(total_ms, 1)})
        send_json({"type": "tts_start", **payload})

    def on_tts_end(payload: dict):
        nonlocal reply_in_progress
        reply_in_progress = False
        send_json({"type": "tts_end", **payload})

    def on_chat_response(content: str, question_id: str, reply_id: str):
        nonlocal last_question_id, last_reply_id, reply_in_progress
        last_question_id = question_id or last_question_id
        last_reply_id = reply_id or last_reply_id
        # ChatResponse is often streamed in chunks; only mark llm_response_start once per reply_id.
        if reply_id and reply_id not in seen_llm_start_reply_ids:
            seen_llm_start_reply_ids.add(reply_id)
            latency_logger.llm_response_start(question_id, reply_id)
            # 以 ChatResponse 的首个 reply_id 作为“本轮模型开始回复”的兜底信号。
            reply_in_progress = True
        if content:
            _append_history("面试官", content)
        send_json(
            {
                "type": "chat",
                "content": content,
                "question_id": question_id,
                "reply_id": reply_id,
            }
        )

    def on_chat_ended(question_id: str, reply_id: str):
        nonlocal auto_followup_waiting_user
        send_json({"type": "chat_end", "question_id": question_id, "reply_id": reply_id})

        # 自动追问逻辑：在 ChatEnded(559) 之后最多触发一次自动 501，直到用户下一轮完整回答。
        LOG.info(
            "Auto-501 decision: ChatEnded; pending=%d waiting_user=%s",
            len(pending_followups),
            auto_followup_waiting_user,
        )
        send_json(
            {
                "type": "auto_501_decision",
                "phase": "chat_ended",
                "pending_len": len(pending_followups),
                "waiting_user": auto_followup_waiting_user,
            }
        )

        if not client:
            return
        if mode == "t2s":
            # t2s：追问已在语音 ASR 完成后与 501 合并为一条，不在 ChatEnded 再发第二条 501。
            return
        if not (auto_501_from_asr_enabled and followup_client.enabled and external_followup_enabled):
            return
        if not pending_followups:
            return
        if auto_followup_waiting_user:
            # 上一次自动追问后用户还没完成一轮新的 ASR，避免连环追问。
            LOG.info("Auto-501 skipped at ChatEnded (still waiting user response).")
            return

        # 情况：当前这轮回答结束，且用户已经对上一轮追问做出回应（或这是第一次追问），
        # 发送下一条追问。发送后标记等待用户新一轮 ASR。
        info = pending_followups.pop(0)
        question = info.get("question", "")
        wrapped = _wrap_followup_for_chat_model(question)
        asyncio.create_task(client.send_text_query(wrapped))
        auto_followup_waiting_user = True
        LOG.info("Auto-501 sent at ChatEnded; question=%s", question)
        send_json(
            {
                "type": "chat_text_query_sent",
                "reason": "external_followup",
                "followup_source_asr": info.get("source_asr", ""),
                "followup_stage": info.get("stage", prompt_manager.stage),
                "trigger": "chat_ended_immediate",
            }
        )

    def on_barge_in(question_id: str):
        nonlocal barge_in_flag
        nonlocal last_question_id
        if _t2s_tts_barge_suppress_active():
            LOG.info("t2s barge_in suppressed (Doubao ASR_INFO during TTS lead-in)")
            push_server_log(
                "info",
                "barge_in suppressed (TTS lead-in window)",
                ms=T2S_TTS_BARGE_SUPPRESS_MS,
            )
            return
        barge_in_flag = True
        last_question_id = question_id or last_question_id
        send_json(
            {
                "type": "barge_in",
                "source": "doubao_realtime",
                "question_id": question_id,
                "reason": "user_speech",
                "reason_label": "用户打断（检测到说话）",
            }
        )

    def on_session_started(dialog_id: str):
        nonlocal dialog_id_latest
        dialog_id_latest = dialog_id or dialog_id_latest
        send_json({"type": "session_started", "dialog_id": dialog_id_latest})

    def on_error(msg: str):
        send_json({"type": "error", "message": msg})

    async def restart_session(reason: str) -> None:
        """Restart Doubao session so new system prompt takes effect immediately."""
        nonlocal barge_in_flag
        if not client:
            return
        async with restart_lock:
            try:
                barge_in_flag = False
                await client.finish_session()
            except Exception:
                pass
            new_sid = await client.start_session(
                system_role=prompt_manager.get_system_role(),
                speaking_style=prompt_manager.get_speaking_style(),
                dialog_id=dialog_id_latest,
                input_mod="audio" if duplex_server_vad else ("text" if mode == "t2s" else "audio"),
                server_vad=duplex_server_vad,
                end_smooth_window_ms=duplex_end_smooth_ms,
                voice=session_voice,
            )
            sessions.get(conn_id, {})["session_id"] = new_sid
            seen_llm_start_reply_ids.clear()
            send_json({"type": "session_restarted", "session_id": new_sid, "reason": reason})

    async def _t2s_trigger_complete(*, source: str) -> None:
        """Server-side equivalent of voice_input_complete (t2s only), with dedupe/guard."""
        nonlocal t2s_complete_task, t2s_voice_active, t2s_eou_fired, t2s_last_complete_ts
        if mode not in {"t2s", "pipeline"}:
            return
        if mode == "t2s" and not client:
            return
        if mode == "t2s" and not t2s_asr_streaming and not t2s_pcm_buffer:
            return
        if source in SEMANTIC_VAD_SOURCES:
            now = time.monotonic()
            if t2s_speech_started_ts > 0 and (now - t2s_speech_started_ts) * 1000.0 < t2s_min_speech_ms:
                return
            if t2s_last_complete_ts > 0 and (now - t2s_last_complete_ts) * 1000.0 < t2s_min_turn_gap_ms:
                return

        if t2s_eou_fired:
            return
        t2s_eou_fired = True
        t2s_last_complete_ts = time.monotonic()
        _dump_t2s_asr_capture_once(source)
        send_json({"type": "voice_input_complete_suggested", "source": source})

        if mode == "pipeline":
            if pipeline_orch:
                asyncio.create_task(pipeline_orch.on_eou())
            return

        if t2s_complete_task and not t2s_complete_task.done():
            return

        async def _run_asr_and_send() -> None:
            nonlocal t2s_asr_streaming, t2s_asr_stream_buffer, asr_client, t2s_voice_active
            nonlocal t2s_vad_segments, t2s_asr_segments, t2s_pending_followup_question
            asr_text_sent = False
            try:
                _cancel_t2s_asr_keepalive()
                session = t2s_asr_streaming
                if session:
                    if t2s_asr_stream_buffer:
                        await session.send_audio(bytes(t2s_asr_stream_buffer), is_last=True)
                        t2s_asr_stream_buffer.clear()
                    else:
                        await session.send_audio(b"", is_last=True)
                    text = await session.wait_final()
                    t2s_asr_streaming = None
                else:
                    if not t2s_pcm_buffer:
                        send_json({"type": "error", "message": "no voice data"})
                        return
                    pcm_bytes = bytes(t2s_pcm_buffer)
                    t2s_pcm_buffer.clear()
                    if asr_client is None:
                        rid = os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
                        asr_client = VolcAsrClient(ASR_APP_ID, ASR_ACCESS_KEY, resource_id=rid)
                    text = await asr_client.transcribe_pcm16le_16k(pcm_bytes, language="zh-CN")

                text = (text or "").strip()
                if not text:
                    asr_err = session.get_last_error_code() if session else None
                    msg = "ASR produced empty text"
                    if asr_err is not None:
                        msg += f" (asr_error_code={asr_err}{asr_error_hint(asr_err)})"
                    send_json({"type": "error", "message": msg})
                    return
                send_json({"type": "local_asr_text", "text": text})

                # 单条 501：先合并「上轮迟到」的追问到本轮 ASR 末尾；再限时等待本轮追问，超时则追问记入 pending 供下一轮末尾合并。
                query_base = (text or "").strip()
                if t2s_pending_followup_question:
                    query_base = _t2s_append_followup_to_asr_text(query_base, t2s_pending_followup_question)
                    send_json(
                        {
                            "type": "followup_deferred_applied",
                            "question": t2s_pending_followup_question,
                            "stage": prompt_manager.stage,
                        }
                    )
                    t2s_pending_followup_question = ""

                merge_wait_s = float(os.environ.get("T2S_FOLLOWUP_MERGE_WAIT_S", "2.5"))
                follow_q_in_time: str | None = None

                if auto_501_from_asr_enabled and external_followup_enabled and followup_client.enabled:
                    followup_capture_turn_id = t2s_turn_id
                    send_json(
                        {
                            "type": "followup_request_start",
                            "source_asr_preview": text[:200] + ("…" if len(text) > 200 else ""),
                            "stage": prompt_manager.stage,
                        }
                    )

                    async def _call_followup() -> str:
                        history_str = "\n".join(dialogue_history[-20:])
                        raw = await followup_client.generate_followup(
                            text,
                            stage=prompt_manager.stage,
                            dialogue_history=history_str or None,
                            output_format="plain",
                        )
                        return (raw or "").strip()

                    fu_task = asyncio.create_task(_call_followup())
                    done, _pending = await asyncio.wait({fu_task}, timeout=merge_wait_s, return_when=asyncio.FIRST_COMPLETED)
                    if fu_task in done:
                        try:
                            follow_q_in_time = fu_task.result()
                        except Exception as exc:  # noqa: BLE001
                            LOG.warning("t2s followup model failed: %s", exc)
                            send_json({"type": "error", "message": f"followup model error: {exc}"})
                            follow_q_in_time = None
                    else:

                        async def _late_followup() -> None:
                            nonlocal t2s_pending_followup_question
                            if not client:
                                return
                            try:
                                fq = await fu_task
                                fq = (fq or "").strip()
                            except Exception as exc:  # noqa: BLE001
                                LOG.warning("t2s late followup failed: %s", exc)
                                send_json({"type": "error", "message": f"followup model error: {exc}"})
                                return
                            if followup_capture_turn_id != t2s_turn_id:
                                LOG.info(
                                    "t2s late followup dropped (stale turn %s, current %s)",
                                    followup_capture_turn_id,
                                    t2s_turn_id,
                                )
                                return
                            if fq:
                                t2s_pending_followup_question = fq
                                send_json(
                                    {
                                        "type": "followup_generated",
                                        "question": fq,
                                        "source_asr": text,
                                        "stage": prompt_manager.stage,
                                        "merged_into_501": False,
                                        "deferred_to_next_asr": True,
                                    }
                                )
                            else:
                                send_json(
                                    {
                                        "type": "followup_skipped",
                                        "reason": "model_decision_no_followup",
                                        "source_asr": text,
                                        "stage": prompt_manager.stage,
                                        "deferred": True,
                                    }
                                )

                        asyncio.create_task(_late_followup())

                    if follow_q_in_time is not None:
                        if follow_q_in_time:
                            query_base = _t2s_append_followup_to_asr_text(query_base, follow_q_in_time)
                            send_json(
                                {
                                    "type": "followup_generated",
                                    "question": follow_q_in_time,
                                    "source_asr": text,
                                    "stage": prompt_manager.stage,
                                    "merged_into_501": True,
                                    "deferred_to_next_asr": False,
                                }
                            )
                        else:
                            send_json(
                                {
                                    "type": "followup_skipped",
                                    "reason": "model_decision_no_followup",
                                    "source_asr": text,
                                    "stage": prompt_manager.stage,
                                }
                            )

                pcm_turn = bytes(t2s_asr_capture_buffer)
                merged = query_base.strip() != (text or "").strip()
                if pcm_turn and not merged:
                    await client.send_audio_turn(pcm_turn)
                    send_reason = "t2s_voice_pcm"
                else:
                    await client.send_text_query(query_base)
                    send_reason = "t2s_voice_asr"
                _append_history("证明人", text)
                asr_text_sent = True
                send_json(
                    {
                        "type": "chat_text_query_sent",
                        "reason": send_reason,
                        "trigger": f"voice_input_complete:{source}",
                        "merged_followup": merged,
                    }
                )
            finally:
                vad_last_turn_wav_b64 = ""
                asr_last_turn_wav_b64 = ""
                if t2s_vad_turn_buffer:
                    snap_v = bytes(t2s_vad_turn_buffer)
                    t2s_vad_turn_pcm_history.append(snap_v)
                    vad_last_turn_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(snap_v)).decode("ascii")
                    t2s_vad_merged_buffer.extend(t2s_vad_turn_buffer)
                    t2s_vad_segments += 1
                    t2s_vad_turn_buffer.clear()
                if t2s_asr_capture_buffer:
                    snap_a = bytes(t2s_asr_capture_buffer)
                    t2s_asr_turn_pcm_history.append(snap_a)
                    asr_last_turn_wav_b64 = base64.b64encode(_pcm16le_to_wav_bytes(snap_a)).decode("ascii")
                    t2s_asr_merged_buffer.extend(t2s_asr_capture_buffer)
                    t2s_asr_segments += 1
                _send_t2s_debug_preview(
                    vad_last_turn_wav_b64=vad_last_turn_wav_b64,
                    asr_last_turn_wav_b64=asr_last_turn_wav_b64,
                )
                t2s_voice_active = False
                send_json(
                    {
                        "type": "voice_turn_finished",
                        "source": source,
                        "asr_text_sent": asr_text_sent,
                    }
                )

        t2s_complete_task = asyncio.create_task(_run_asr_and_send())

    async def _cleanup_t2s_on_ws_close() -> None:
        """Stop Volc ASR and t2s tasks when the browser closes WS (e.g. Stop); avoids 45000081 hang."""
        nonlocal t2s_asr_streaming, t2s_complete_task
        _cancel_t2s_speak_grace()
        if t2s_complete_task and not t2s_complete_task.done():
            t2s_complete_task.cancel()
            try:
                await t2s_complete_task
            except (asyncio.CancelledError, Exception):
                pass
        t2s_complete_task = None
        _cancel_t2s_asr_keepalive()
        sess = t2s_asr_streaming
        t2s_asr_streaming = None
        if sess:
            try:
                await sess.abort()
            except Exception:
                pass

    async def _t2s_teardown_voice_lane(*, log_reason: str = "") -> None:
        """Abort streaming ASR and close VAD so the next voice_input_start can switch provider."""
        nonlocal t2s_asr_streaming, t2s_vad, t2s_voice_active
        if log_reason:
            LOG.info("t2s voice lane teardown: %s", log_reason)
        _cancel_t2s_asr_keepalive()
        sess = t2s_asr_streaming
        t2s_asr_streaming = None
        if sess:
            try:
                await sess.abort()
            except Exception:
                pass
        if t2s_vad is not None:
            try:
                await t2s_vad.close_session()
            except Exception:
                pass
            t2s_vad = None
        t2s_voice_active = False
        if mode == "pipeline" and pipeline_orch:
            try:
                await pipeline_orch.abort_uplink()
            except Exception:
                LOG.debug("pipeline abort_uplink failed", exc_info=True)

    async def _t2s_start_voice_input(
        vad_provider_override: str | None = None,
        stt_backend_override: str | None = None,
        livekit_turn_threshold_override: str | float | None = None,
    ) -> bool:
        """Initialize VAD + streaming ASR; notify client via voice_input_started when ready."""
        nonlocal t2s_asr_streaming, t2s_asr_stream_buffer, t2s_pcm_buffer, t2s_voice_active
        nonlocal t2s_speech_started, t2s_eou_fired, t2s_speech_started_ts, t2s_last_nonidle_ts
        nonlocal t2s_turn_id, t2s_vad_idle_streak, t2s_asr_gate_open
        nonlocal t2s_pre_roll_chunks, t2s_asr_capture_buffer, t2s_vad_turn_buffer, t2s_vad
        nonlocal t2s_asr_last_real_send_monotonic, t2s_asr_last_keepalive_monotonic, t2s_asr_keepalive_task

        if mode == "pipeline":
            if not pipeline_orch:
                send_json({"type": "error", "message": "voice_input_start only valid after pipeline session ready"})
                return False
        elif not client or mode != "t2s":
            send_json({"type": "error", "message": "voice_input_start only valid in t2s mode"})
            return False
        if t2s_asr_streaming or t2s_vad is not None:
            await _t2s_teardown_voice_lane(log_reason="restart voice_input_start")

        t2s_asr_stream_buffer.clear()
        t2s_pcm_buffer.clear()
        t2s_voice_active = True
        t2s_speech_started = False
        t2s_eou_fired = False
        t2s_speech_started_ts = 0.0
        t2s_last_nonidle_ts = 0.0
        _cancel_t2s_speak_grace()
        t2s_turn_id += 1
        t2s_vad_idle_streak = 0
        t2s_asr_gate_open = False
        t2s_pre_roll_chunks.clear()
        t2s_asr_capture_buffer.clear()
        t2s_vad_turn_buffer.clear()
        _close_t2s_debug_files()
        _open_t2s_debug_files_for_turn()
        active_vad = resolve_vad_provider(override=vad_provider_override)
        LOG.info(
            "t2s voice_input_start: vad_provider=%r resolved=%s livekit_stt=%r livekit_threshold=%r",
            vad_provider_override,
            active_vad,
            stt_backend_override,
            livekit_turn_threshold_override,
        )
        t2s_vad = create_vad_backend(
            active_vad,
            livekit_stt_backend=stt_backend_override if active_vad == "livekit_turn_detector" else None,
            livekit_turn_threshold=livekit_turn_threshold_override
            if active_vad == "livekit_turn_detector"
            else None,
        )
        if t2s_vad is not None:
            try:
                vad_status = await t2s_vad.start_session(uuid.uuid4().hex)
                send_json({"type": "vad_status", **vad_status})
            except Exception as e:
                LOG.warning("VAD start_session failed: %s", e)
                push_server_log("warning", f"VAD start_session failed: {e}")
                provider = getattr(t2s_vad, "provider", active_vad)
                await t2s_vad.close_session()
                t2s_vad = None
                t2s_voice_active = False
                send_json(
                    {
                        "type": "vad_status",
                        **vad_status_on_init_failure(provider, str(e)),
                    }
                )
                send_json({"type": "voice_input_failed", "reason": "vad_init", "message": str(e)})
                return False
        else:
            t2s_voice_active = False
            send_json(
                {
                    "type": "vad_status",
                    **vad_status_on_init_failure(
                        active_vad,
                        "VAD backend unavailable (check provider / dependencies)",
                    ),
                }
            )
            send_json(
                {
                    "type": "voice_input_failed",
                    "reason": "vad_unavailable",
                    "message": "VAD backend unavailable",
                }
            )
            return False

        async def _on_asr_partial(text: str, is_interim: bool) -> None:
            send_json({"type": "asr", "text": text, "is_interim": is_interim})
            if t2s_vad is not None and hasattr(t2s_vad, "update_user_transcript"):
                t2s_vad.update_user_transcript(text)

        async def _on_asr_server_error(code: int | None) -> None:
            hint = asr_error_hint(code) if code is not None else ""
            msg = (
                f"ASR server error code={code}"
                if code is not None
                else "ASR server error (no code in frame)"
            )
            if hint:
                msg += f" — {hint.strip()}"
            push_server_log("warning", msg, asr_error_code=code)

        async def _on_asr_notify(level: str, message: str) -> None:
            push_server_log(level, message)

        if mode == "pipeline":
            try:
                await pipeline_orch.start_uplink()
            except Exception as e:
                LOG.warning("pipeline ASR start failed: %s", e)
                push_server_log("error", f"pipeline ASR start failed: {e}")
                t2s_voice_active = False
                if t2s_vad is not None:
                    try:
                        await t2s_vad.close_session()
                    except Exception:
                        pass
                    t2s_vad = None
                send_json(
                    {
                        "type": "error",
                        "message": (
                            f"ASR 连接失败: {e}. 排查：config.json asr_app_id / asr_access_key "
                            "或 ASR_APP_ID / ASR_ACCESS_KEY"
                        ),
                    }
                )
                send_json({"type": "voice_input_failed", "reason": "asr_init", "message": str(e)})
                return False
            send_json({"type": "voice_input_started"})
            return True

        use_livekit_stt = active_vad == "livekit_turn_detector"
        if use_livekit_stt:
            resolved_stt = resolve_livekit_turn_stt_backend(override=stt_backend_override)
            session = create_livekit_turn_stt_session(
                stt_backend=stt_backend_override,
                on_partial=_on_asr_partial,
                on_notify=_on_asr_notify,
                on_server_error=_on_asr_server_error,
            )
            stt_label = livekit_turn_stt_display_label(resolved_stt)
        else:
            rid = os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
            session = VolcAsrStreamingSession(
                ASR_APP_ID,
                ASR_ACCESS_KEY,
                resource_id=rid,
                language="zh-CN",
                on_partial=_on_asr_partial,
                on_server_error=_on_asr_server_error,
                on_notify=_on_asr_notify,
            )
            stt_label = "Volc ASR"
        try:
            await session.start()
        except Exception as e:
            LOG.warning("%s streaming start failed: %s", stt_label, e)
            push_server_log("error", f"{stt_label} streaming start failed: {e}")
            t2s_voice_active = False
            if t2s_vad is not None:
                try:
                    await t2s_vad.close_session()
                except Exception:
                    pass
                t2s_vad = None
            if use_livekit_stt:
                resolved_stt = resolve_livekit_turn_stt_backend(override=stt_backend_override)
                if resolved_stt == "dashscope":
                    err_msg = (
                        f"DashScope Paraformer ASR 连接失败: {e}. "
                        "排查：1) 设置 DASHSCOPE_API_KEY 2) pip install dashscope "
                        "3) 可选 LIVEKIT_TURN_STT=dashscope / vad.livekit_turn.stt_backend"
                    )
                elif resolved_stt == "funasr":
                    err_msg = (
                        f"FunASR SenseVoice 启动失败: {e}. "
                        "请安装: pip install funasr modelscope torch torchaudio "
                        "（CPU: pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu）"
                        "；首次运行会自动下载 iic/SenseVoiceSmall。"
                    )
                elif resolved_stt == "volc":
                    err_msg = (
                        f"豆包流式 ASR 连接失败: {e}. 排查：1) 火山控制台-豆包语音-语音识别大模型 已开通 "
                        "2) ASR_APP_ID / ASR_ACCESS_KEY 或 config.json asr_app_id / asr_access_key"
                    )
                elif resolved_stt == "livekit_inference":
                    err_msg = (
                        f"LiveKit Inference STT 连接失败: {e}. "
                        "请配置 LIVEKIT_API_KEY + LIVEKIT_API_SECRET。"
                    )
                else:
                    err_msg = f"LiveKit turn STT ({resolved_stt}) 连接失败: {e}"
            else:
                err_msg = (
                    f"ASR 连接失败 (403): {e}. 排查：1) 火山控制台-豆包语音-语音识别大模型 已开通 "
                    "2) 若 ASR 与语音对话用不同应用，在 config.json 添加 asr_app_id、asr_access_key "
                    "3) 尝试 ASR_AUTH_STYLE=voice 或 ASR_RESOURCE_ID=volc.bigasr.sauc.concurrent"
                )
            send_json({"type": "error", "message": err_msg})
            send_json({"type": "voice_input_failed", "reason": "asr_init", "message": str(e)})
            return False

        _cancel_t2s_asr_keepalive()
        t2s_asr_last_real_send_monotonic = 0.0
        t2s_asr_last_keepalive_monotonic = 0.0
        t2s_asr_streaming = session
        t2s_asr_keepalive_task = asyncio.create_task(_t2s_asr_keepalive_loop(session))
        send_json({"type": "voice_input_started"})
        return True

    try:
        raw = await websocket.receive_text()
        try:
            cfg = json.loads(raw)
            # 前端应该传入 mode，但为了防止异常情况，这里做一层兜底：
            # - 优先使用 cfg.mode
            # - 若空，则根据 Referer 是否包含 /t2sbg 推断为 t2s
            # - 否则退回 e2e
            referer = websocket.headers.get("referer") or ""
            cfg_mode = (cfg.get("mode") or "").strip()
            if cfg_mode:
                mode = cfg_mode
            elif "/t2sbg-pipeline" in referer:
                mode = "pipeline"
            elif "/t2sbg" in referer:
                mode = "t2s"
            else:
                mode = "e2e"

            if mode in {"t2s", "pipeline"} and hush_enabled():
                t2s_hush = create_hush_session()
                if t2s_hush is not None:
                    LOG.info("t2s Hush denoiser session ready")
                elif hush_manager_error():
                    LOG.warning("t2s Hush denoiser unavailable: %s", hush_manager_error())

            stage = cfg.get("stage", "introduction")
            prompt_manager.set_stage(stage)
            # Independent demo toggle for "auto 501 from ASR" behavior.
            auto_501_from_asr_enabled = bool(cfg.get("auto_501_from_asr_enabled", True))
            # t2sbg：追问链路暂时关闭；其他 mode 默认开启，可由客户端显式覆盖。
            # t2sbg / pipeline：追问链路默认关闭
            external_followup_enabled = bool(cfg.get("external_followup_enabled", mode not in {"t2s", "pipeline"}))
            if cfg.get("verification_case") or cfg.get("system_role"):
                prompt_manager.set_custom_prompt(
                    system_role=cfg.get("system_role"),
                    speaking_style=cfg.get("speaking_style"),
                    verification_case=cfg.get("verification_case"),
                )
            duplex_server_vad = _resolve_duplex_server_vad(cfg, DEFAULT_DUPLEX_SERVER_VAD)
            if duplex_server_vad and mode not in {"semi", "pipeline"}:
                mode = "e2e"
            raw_voice = (cfg.get("voice") or cfg.get("voice_type") or "").strip()
            if raw_voice:
                from backend.doubao_voices import resolve_voice

                try:
                    session_voice = resolve_voice(raw_voice, default=_WS_DEFAULT_SPEAKER)
                except ValueError as exc:
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
                    )
                    await websocket.close()
                    return
        except json.JSONDecodeError:
            pass

        if mode == "pipeline":
            from backend.pipeline.adapters.asr_volc import VolcAsrAdapter
            from backend.pipeline.config import (
                build_streaming_llm,
                build_streaming_tts,
                load_pipeline_settings,
            )
            from backend.pipeline.orchestrator import PipelineOrchestrator

            pipeline_settings = load_pipeline_settings()
            cloud_llm = pipeline_settings.llm_adapter not in {"stub", "llm_stub"}
            cloud_tts = pipeline_settings.tts_adapter not in {"stub", "tts_stub"}
            if (cloud_llm or cloud_tts) and not pipeline_settings.api_key:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                "Pipeline LLM/TTS needs DASHSCOPE_API_KEY "
                                "(default host https://dashscope.aliyuncs.com; "
                                "optional PIPELINE_LLM_BASE_URL / PIPELINE_TTS_WS_URL)."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                await websocket.close()
                return

            def _pipeline_asr_factory():
                rid = os.environ.get("ASR_RESOURCE_ID", DEFAULT_ASR_RESOURCE_ID)
                return VolcAsrAdapter(ASR_APP_ID, ASR_ACCESS_KEY, resource_id=rid)

            async def _pipeline_emit_pcm(data: bytes) -> None:
                await _send_bytes_safe(data)

            pipeline_voice = pipeline_settings.tts_voice or session_voice
            try:
                pipeline_llm = build_streaming_llm(pipeline_settings)
                pipeline_tts = build_streaming_tts(pipeline_settings)
            except Exception as exc:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
                )
                await websocket.close()
                return

            pipeline_orch = PipelineOrchestrator(
                llm=pipeline_llm,
                tts=pipeline_tts,
                asr_factory=_pipeline_asr_factory,
                emit_json=send_json,
                emit_pcm=_pipeline_emit_pcm,
                system_role=prompt_manager.get_system_role(),
                voice=pipeline_voice,
                tts_barge_suppress_ms=T2S_TTS_BARGE_SUPPRESS_MS,
            )
            session_id = str(uuid.uuid4())
            sessions[conn_id] = {
                "client": None,
                "session_id": session_id,
                "mode": mode,
                "prompt_manager": prompt_manager,
                "latency_logger": latency_logger,
                "pipeline": pipeline_orch,
            }
            send_json(
                {
                    "type": "start_session_config",
                    "mode": mode,
                    "input_mod": "asr_llm_tts",
                    "session_id": session_id,
                    "voice": pipeline_voice,
                    "api_version": "pipeline-dashscope",
                    "model": pipeline_settings.llm_model,
                    "tts_model": pipeline_settings.tts_model,
                    "llm_adapter": pipeline_settings.llm_adapter,
                    "tts_adapter": pipeline_settings.tts_adapter,
                    "enable_thinking": pipeline_settings.enable_thinking,
                    "followup": {"enabled": False},
                    "auto_501_from_asr_enabled": False,
                    "external_followup_enabled": False,
                    "followup_pipeline_active": False,
                }
            )
            send_json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "mode": mode,
                    "duplex_server_vad": False,
                    "recording_ready": False,
                    "followup": {"enabled": False},
                    "hush": hush_status_payload(
                        ready=t2s_hush is not None,
                        error=hush_manager_error() if hush_enabled() and t2s_hush is None else "",
                    ),
                }
            )
            send_json(
                {
                    "type": "session_preparing",
                    "stage": "vad_asr",
                    "message": "等待客户端启动 VAD 与语音识别…",
                }
            )
        else:
            if not DOUBAO_API_KEY and not (APP_ID and ACCESS_KEY):
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": (
                                "Missing Doubao credentials. Set DOUBAO_API_KEY (new console, X-Api-Key) "
                                "or DOUBAO_APP_ID + DOUBAO_ACCESS_KEY (legacy console)."
                            ),
                        }
                    )
                )
                await websocket.close()
                return

            client = DuplexVoiceClient(
                api_key=DOUBAO_API_KEY,
                app_id=APP_ID,
                access_key=ACCESS_KEY,
                on_asr=on_asr,
                on_asr_ended=on_asr_ended,
                on_tts_audio=on_tts_audio,
                on_tts_start=on_tts_start,
                on_tts_end=on_tts_end,
                on_chat_response=on_chat_response,
                on_chat_ended=on_chat_ended,
                on_barge_in=on_barge_in,
                on_session_started=on_session_started,
                on_error=on_error,
            )
            await client.connect()
            input_mod = "audio" if duplex_server_vad else ("text" if mode == "t2s" else "audio")
            duplex_input_mod = input_mod
            LOG.info(
                "StartSession with mode=%s input_mod=%s duplex_server_vad=%s voice=%s",
                mode,
                input_mod,
                duplex_server_vad,
                session_voice,
            )
            session_id = await client.start_session(
                system_role=prompt_manager.get_system_role(),
                speaking_style=prompt_manager.get_speaking_style(),
                input_mod=input_mod,
                server_vad=duplex_server_vad,
                end_smooth_window_ms=duplex_end_smooth_ms,
                voice=session_voice,
            )
            client.start_receive_loop()
            sessions[conn_id] = {
                "client": client,
                "session_id": session_id,
                "mode": mode,
                "prompt_manager": prompt_manager,
                "latency_logger": latency_logger,
            }
            send_json(
                {
                    "type": "start_session_config",
                    "mode": mode,
                    "input_mod": input_mod,
                    "duplex_input_mod": duplex_input_mod,
                    "duplex_server_vad": duplex_server_vad,
                    "end_smooth_window_ms": duplex_end_smooth_ms,
                    "session_id": session_id,
                    "voice": session_voice,
                    "api_version": "duplex-3.0",
                    "auth_mode": client.auth_mode(),
                    "model": "1.2.6.0",
                    "followup": followup_client.describe_for_client(),
                    "auto_501_from_asr_enabled": auto_501_from_asr_enabled,
                    "external_followup_enabled": external_followup_enabled,
                    "followup_pipeline_active": bool(
                        mode == "t2s"
                        and auto_501_from_asr_enabled
                        and external_followup_enabled
                        and followup_client.enabled
                    ),
                }
            )
            send_json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "mode": mode,
                    "duplex_server_vad": duplex_server_vad,
                    "recording_ready": mode != "t2s" or duplex_server_vad,
                    "followup": followup_client.describe_for_client(),
                    "auto_501_from_asr_enabled": auto_501_from_asr_enabled,
                    "external_followup_enabled": external_followup_enabled,
                    "followup_pipeline_active": bool(
                        mode == "t2s"
                        and auto_501_from_asr_enabled
                        and external_followup_enabled
                        and followup_client.enabled
                    ),
                    "hush": hush_status_payload(
                        ready=t2s_hush is not None,
                        error=hush_manager_error() if mode == "t2s" and hush_enabled() and t2s_hush is None else "",
                    )
                    if mode == "t2s"
                    else hush_status_payload(ready=False, error=""),
                }
            )
            if mode == "t2s" and not duplex_server_vad:
                send_json(
                    {
                        "type": "session_preparing",
                        "stage": "vad_asr",
                        "message": "等待客户端启动 VAD 与语音识别…",
                    }
                )

        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                # Starlette raises this if we call receive() after a disconnect message.
                if 'Cannot call "receive" once a disconnect message has been received.' in str(e):
                    break
                raise
            if "bytes" in msg and msg["bytes"]:
                data = msg["bytes"]
                if mode == "pipeline":
                    if not pipeline_orch:
                        continue
                elif not client or client._session_id is None:
                    continue
                if mode in {"t2s", "pipeline"}:
                    data = _normalize_input_audio_to_16k(data)
                    if not data:
                        continue
                    data = _denoise_t2s_uplink_pcm(data)
                    if not data:
                        continue
                chunks = pcm_chunks_from_bytes(data)
                for ch in chunks:
                    if mode in {"t2s", "pipeline"}:
                        # After EoU/completion fires, discard any late mic packets until next start.
                        if t2s_eou_fired:
                            continue

                        chunk_forwarded_by_preroll = False
                        if t2s_voice_active and not t2s_eou_fired:
                            t2s_vad_turn_buffer.extend(ch)
                            if t2s_debug_vad_file is not None:
                                try:
                                    t2s_debug_vad_file.write(ch)
                                except Exception:
                                    pass
                            t2s_pre_roll_chunks.append(ch)
                            if t2s_vad is not None:
                                vad_events = await t2s_vad.feed_pcm_s16le_16k(ch)
                                for vad_ev in vad_events:
                                    if await _apply_t2s_vad_event(vad_ev):
                                        chunk_forwarded_by_preroll = True

                        if (
                            t2s_voice_active
                            and t2s_asr_gate_open
                            and not t2s_eou_fired
                            and not chunk_forwarded_by_preroll
                        ):
                            await _forward_split_asr_pcm(ch)
                    else:
                        await client.send_audio(ch)
            elif "text" in msg and msg["text"]:
                try:
                    obj = json.loads(msg["text"])
                    if obj.get("type") == "monitor_publish":
                        ft_raw = obj.get("full_turn_wavs_base64")
                        ft_list = [str(x) for x in ft_raw] if isinstance(ft_raw, list) else []
                        snap = _build_monitor_snapshot(
                            transcript_html=str(obj.get("transcript_html") or ""),
                            log_html=str(obj.get("log_html") or ""),
                            full_merged_wav_b64=str(obj.get("full_merged_wav_base64") or ""),
                            full_turn_wavs_b64=[x for x in ft_list if x],
                        )
                        await broadcast_t2sbg_monitor(snap)
                        async with _monitor_clients_lock:
                            n_sub = len(_monitor_clients)
                        send_json({"type": "monitor_publish_ok", "monitor_subscribers": n_sub})
                    elif obj.get("type") == "update_prompt":
                        prompt_manager.set_custom_prompt(
                            system_role=obj.get("system_role"),
                            speaking_style=obj.get("speaking_style"),
                            verification_case=obj.get("verification_case"),
                        )
                        if mode == "pipeline" and pipeline_orch:
                            pipeline_orch.set_system_role(prompt_manager.get_system_role())
                            send_json({"type": "session_restarted", "reason": "update_prompt"})
                        else:
                            asyncio.create_task(restart_session("update_prompt"))
                    elif obj.get("type") == "set_stage":
                        prompt_manager.set_stage(obj.get("stage", "introduction"))
                        if mode == "pipeline" and pipeline_orch:
                            pipeline_orch.set_system_role(prompt_manager.get_system_role())
                            send_json({"type": "session_restarted", "reason": "set_stage"})
                        else:
                            asyncio.create_task(restart_session("set_stage"))
                    elif obj.get("type") == "config_audio":
                        if mode not in {"t2s", "pipeline"}:
                            continue
                        sr = _set_t2s_input_sample_rate(
                            obj.get("input_sample_rate", obj.get("sample_rate", VAD_ASR_TARGET_SAMPLE_RATE))
                        )
                        send_json(
                            {
                                "type": "audio_config_applied",
                                "input_sample_rate": sr,
                                "vad_asr_target_sample_rate": VAD_ASR_TARGET_SAMPLE_RATE,
                                "resample_enabled": bool(sr != VAD_ASR_TARGET_SAMPLE_RATE),
                                "resampler": "soxr" if soxr is not None else "linear_fallback",
                            }
                        )
                        push_server_log(
                            "info",
                            "audio_config applied",
                            input_sample_rate=sr,
                            target_sample_rate=VAD_ASR_TARGET_SAMPLE_RATE,
                            resample_enabled=bool(sr != VAD_ASR_TARGET_SAMPLE_RATE),
                            resampler="soxr" if soxr is not None else "linear_fallback",
                        )
                    elif obj.get("type") == "user_speech_end":
                        latency_logger.user_speech_end(obj.get("question_id", ""))
                    elif obj.get("type") == "conversation_update":
                        if not client:
                            send_json({"type": "error", "message": "client not ready"})
                            continue
                        target = obj.get("target", "")
                        new_text = obj.get("text", "")
                        if target == "question":
                            item_id = last_question_id
                        elif target == "reply":
                            item_id = last_reply_id
                        else:
                            item_id = obj.get("item_id", "")
                        if not item_id:
                            send_json({"type": "error", "message": "missing item_id (no last question/reply yet)"})
                            continue
                        await client.conversation_update(item_id=item_id, text=new_text)
                        send_json({"type": "conversation_updated", "target": target, "item_id": item_id})
                    elif obj.get("type") == "soft_prompt_update":
                        # Deprecated: removed from demo UI.
                        send_json({"type": "error", "message": "soft_prompt_update is disabled in this demo"})
                    elif obj.get("type") == "chat_text_query":
                        content = obj.get("content", "") or ""
                        if not content.strip():
                            send_json({"type": "error", "message": "missing content"})
                            continue
                        if mode == "pipeline":
                            if not pipeline_orch:
                                send_json({"type": "error", "message": "client not ready"})
                                continue
                            await pipeline_orch.on_text_query(content)
                            continue
                        if not client:
                            send_json({"type": "error", "message": "client not ready"})
                            continue
                        await client.send_text_query(content)
                        if mode == "t2s":
                            _append_history("证明人", content.strip())
                        send_json({"type": "chat_text_query_sent"})
                    elif obj.get("type") == "voice_input_start":
                        # t2s 流式：开启麦克风流式输入 → ASR 流式输出（后续轮次由客户端触发）
                        vp = (obj.get("vad_provider") or obj.get("local_vad_provider") or "").strip()
                        stt_be = (
                            obj.get("livekit_stt_backend")
                            or obj.get("stt_backend")
                            or ""
                        ).strip()
                        lk_th = obj.get("livekit_turn_threshold")
                        if lk_th is None:
                            lk_th = obj.get("threshold")
                        await _t2s_start_voice_input(vp or None, stt_be or None, lk_th)
                    elif obj.get("type") == "voice_input_complete":
                        if mode not in {"t2s", "pipeline"}:
                            send_json({"type": "error", "message": "voice_input_complete only valid in t2s/pipeline mode"})
                            continue
                        # 默认关闭“本地能量 VAD 自动结束”入口：仅允许显式 manual 触发。
                        complete_source = (obj.get("source", "") or "").strip().lower()
                        if complete_source not in {"manual", "user"}:
                            send_json(
                                {
                                    "type": "voice_input_complete_ignored",
                                    "reason": "local_energy_vad_disabled",
                                    "source": complete_source or "unknown",
                                }
                            )
                            continue
                        asyncio.create_task(_t2s_trigger_complete(source="manual"))
                    elif obj.get("type") == "conversation_create":
                        # Manual (510) context append for testing: write a rule/fact/instruction into history.
                        if not client:
                            send_json({"type": "error", "message": "client not ready"})
                            continue
                        role = obj.get("role", "assistant") or "assistant"
                        text = obj.get("text", "") or ""
                        if not text.strip():
                            send_json({"type": "error", "message": "missing text"})
                            continue
                        await client.conversation_create(role=role, text=text)
                        send_json({"type": "conversation_created", "role": role, "note": "local_ack_only"})
                    elif obj.get("type") == "set_semi_controls":
                        # Runtime toggle for "auto 501 from ASR" behavior.
                        auto_501_from_asr_enabled = bool(
                            obj.get("auto_501_from_asr_enabled", auto_501_from_asr_enabled)
                        )
                    elif obj.get("type") == "set_external_followup":
                        # Runtime toggle for external follow-up model usage.
                        external_followup_enabled = bool(
                            obj.get("external_followup_enabled", external_followup_enabled)
                        )
                        send_json(
                            {
                                "type": "external_followup_updated",
                                "enabled": external_followup_enabled,
                                "has_model": followup_client.enabled,
                            }
                        )
                    elif obj.get("type") == "set_next_turn_501":
                        # One-shot: send a specific 501 content on the next ASREnded.
                        next_turn_501_enabled = bool(obj.get("enabled", False))
                        next_turn_501_content = (obj.get("content", "") or "")
                        send_json(
                            {
                                "type": "next_turn_501_updated",
                                "enabled": next_turn_501_enabled,
                                "has_content": bool(next_turn_501_content.strip()),
                            }
                        )
                except json.JSONDecodeError:
                    pass
    except WebSocketDisconnect:
        LOG.info("WebSocket disconnected %s", conn_id)
    except Exception as e:
        LOG.exception("WS handler error: %s", e)
        try:
            send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if conn_id in sessions:
            del sessions[conn_id]
        try:
            await _cleanup_t2s_on_ws_close()
        except Exception as exc:
            LOG.debug("t2s ASR cleanup on disconnect: %s", exc)
        if t2s_vad is not None:
            try:
                await t2s_vad.close_session()
            except Exception:
                pass
        if t2s_hush is not None:
            try:
                t2s_hush.close()
            except Exception:
                pass
            t2s_hush = None
        _close_t2s_debug_files()
        if pipeline_orch:
            try:
                await pipeline_orch.close()
            except Exception:
                LOG.debug("pipeline close failed", exc_info=True)
        if client:
            await client.disconnect()


# Mount Gradio at /gradio (optional)
try:
    import gradio as gr
    from frontend.gradio_app import ui_blocks, ui_backcheck, ui_t2s_backcheck

    # 原有技术面试控制台
    _gradio_demo = ui_blocks()
    app = gr.mount_gradio_app(app, _gradio_demo, path="/gradio")

    # 新增：简洁版芝麻背调访谈展示页
    _bg_demo = ui_backcheck()
    app = gr.mount_gradio_app(app, _bg_demo, path="/bg")

    # T2S 背调访谈：纯文本输入 → 语音回复
    _t2s_bg_demo = ui_t2s_backcheck()
    app = gr.mount_gradio_app(app, _t2s_bg_demo, path="/t2sbg")
except ImportError:
    pass
