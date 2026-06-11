# -*- coding: utf-8 -*-
"""
LiveKit Inference STT — official Deepgram path from LiveKit docs.

Docs: https://docs.livekit.io/agents/models/stt/deepgram/
Uses LiveKit Cloud gateway (no separate Deepgram API key):

  stt=inference.STT(model="deepgram/nova-3", language="multi")

Requires LIVEKIT_API_KEY + LIVEKIT_API_SECRET (LiveKit Cloud project).
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

LOG = logging.getLogger(__name__)

DEFAULT_INFERENCE_URL = "https://agent-gateway.livekit.cloud/v1"
STAGING_INFERENCE_URL = "https://agent-gateway.staging.livekit.cloud/v1"
CHUNK_MS = 200
CHUNK_BYTES = (16000 * CHUNK_MS // 1000) * 2


def _load_livekit_turn_json() -> dict:
    try:
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            block = (data.get("vad") or {}).get("livekit_turn") or {}
            if isinstance(block, dict):
                return block
    except Exception:
        pass
    return {}


def livekit_api_key() -> str:
    return (os.environ.get("LIVEKIT_API_KEY", "") or "").strip()


def livekit_api_secret() -> str:
    return (os.environ.get("LIVEKIT_API_SECRET", "") or "").strip()


def livekit_inference_url() -> str:
    env = (os.environ.get("LIVEKIT_INFERENCE_URL", "") or "").strip()
    if env:
        return env.rstrip("/")
    livekit_url = (os.environ.get("LIVEKIT_URL", "") or "").strip()
    if ".staging.livekit.cloud" in livekit_url:
        return STAGING_INFERENCE_URL
    cfg = _load_livekit_turn_json()
    cfg_url = str(cfg.get("inference_url", "") or "").strip()
    if cfg_url:
        return cfg_url.rstrip("/")
    return DEFAULT_INFERENCE_URL


def livekit_inference_stt_model() -> str:
    env = (os.environ.get("LIVEKIT_INFERENCE_STT_MODEL", "") or "").strip()
    if env:
        return env
    cfg = _load_livekit_turn_json()
    return str(cfg.get("inference_stt_model", "") or "deepgram/nova-3")


def livekit_inference_stt_language() -> str:
    env = (os.environ.get("LIVEKIT_INFERENCE_STT_LANGUAGE", "") or "").strip()
    if env:
        return env
    cfg = _load_livekit_turn_json()
    return str(cfg.get("inference_stt_language", "") or "multi")


def livekit_inference_credentials_available() -> bool:
    return bool(livekit_api_key() and livekit_api_secret())


def _create_inference_jwt(*, api_key: str, api_secret: str, ttl_sec: int = 600) -> str:
    try:
        from livekit import api

        grant = api.access_token.InferenceGrants(perform=True)
        return (
            api.AccessToken(api_key, api_secret)
            .with_identity("agent")
            .with_inference_grants(grant)
            .with_ttl(datetime.timedelta(seconds=ttl_sec))
            .to_jwt()
        )
    except ImportError as exc:
        raise RuntimeError(
            "livekit-api package is required for LiveKit Inference STT; "
            "install with: pip install livekit-api"
        ) from exc


class LivekitInferenceStreamingSession:
    """LiveKit Inference STT WebSocket (deepgram/nova-3 via agent-gateway)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_notify: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.model = (model or livekit_inference_stt_model()).strip()
        self.language = (language or livekit_inference_stt_language()).strip()
        self.on_partial = on_partial
        self.on_notify = on_notify
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._final_text = ""
        self._done = asyncio.Event()
        self._session_ready = asyncio.Event()

    async def start(self) -> None:
        api_key = livekit_api_key()
        api_secret = livekit_api_secret()
        if not api_key or not api_secret:
            raise RuntimeError(
                "LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required for LiveKit Inference STT "
                "(see https://docs.livekit.io/agents/models/stt/deepgram/)"
            )

        base = livekit_inference_url()
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://") :]
        else:
            ws_base = base
        url = f"{ws_base}/stt?model={self.model}"
        token = _create_inference_jwt(api_key=api_key, api_secret=api_secret)

        self._final_text = ""
        self._session_ready.clear()
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "realtime-voice-interviewer-demo/livekit-inference-stt",
            },
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        session_create = {
            "type": "session.create",
            "model": self.model,
            "settings": {
                "sample_rate": "16000",
                "encoding": "pcm_s16le",
                "language": self.language,
                "extra": {
                    "filler_words": True,
                    "interim_results": True,
                    "punctuate": True,
                    "smart_format": False,
                    "no_delay": True,
                    "endpointing": 25,
                },
            },
        }
        await self._ws.send(json.dumps(session_create))
        self._recv_task = asyncio.create_task(self._recv_loop())
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            await self.abort()
            raise RuntimeError("LiveKit Inference STT session.created timeout") from exc

    async def send_audio(self, pcm_chunk: bytes, is_last: bool = False) -> None:
        if not self._ws:
            return
        if pcm_chunk:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "input_audio",
                        "audio": base64.b64encode(pcm_chunk).decode("ascii"),
                    }
                )
            )
        if is_last:
            await self._ws.send(json.dumps({"type": "session.finalize"}))

    async def send_keepalive(self) -> None:
        # Inference gateway uses WebSocket ping; no Deepgram-style KeepAlive frame.
        if self._ws:
            await self._ws.ping()

    async def abort(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._done.set()

    async def wait_final(self, timeout: float = 5.0) -> str:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._final_text

    async def _recv_loop(self) -> None:
        try:
            assert self._ws is not None
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type")
                if msg_type == "session.created":
                    self._session_ready.set()
                elif msg_type in {"interim_transcript", "final_transcript"}:
                    text = str(data.get("transcript", "") or "").strip()
                    is_final = msg_type == "final_transcript"
                    if text:
                        self._final_text = text
                        if self.on_partial:
                            await self.on_partial(text, not is_final)
                elif msg_type == "session.finalized":
                    break
                elif msg_type == "session.closed":
                    break
                elif msg_type == "error":
                    err = data.get("message") or str(data)
                    LOG.warning("LiveKit Inference STT error: %s", err)
                    if self.on_notify:
                        await self.on_notify("warning", f"LiveKit Inference STT error: {err}")
                    break
            self._done.set()
        except asyncio.CancelledError:
            self._done.set()
            raise
        except ConnectionClosed as exc:
            if exc.code != 1000:
                LOG.warning("LiveKit Inference STT connection closed: %s", exc)
            self._done.set()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("LiveKit Inference STT recv_loop error: %s", exc)
            if self.on_notify:
                try:
                    await self.on_notify("warning", f"LiveKit Inference STT error: {exc}")
                except Exception:
                    pass
            self._done.set()
