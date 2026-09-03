# -*- coding: utf-8 -*-
"""DashScope Paraformer realtime ASR (online) for LiveKit turn-detector STT."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

LOG = logging.getLogger(__name__)

CHUNK_MS = 200
CHUNK_BYTES = (16000 * CHUNK_MS // 1000) * 2
DEFAULT_MODEL = "paraformer-realtime-v2"
DEFAULT_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_json() -> dict:
    path = _root() / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _livekit_turn_cfg() -> dict:
    block = (_load_json().get("vad") or {}).get("livekit_turn") or {}
    return block if isinstance(block, dict) else {}


def dashscope_asr_api_key() -> str:
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if key:
        return key
    bailian = (_load_json().get("followup") or {}).get("bailian") or {}
    if isinstance(bailian, dict):
        return str(bailian.get("api_key") or "").strip()
    return ""


def dashscope_asr_model() -> str:
    env = (
        os.environ.get("DASHSCOPE_ASR_MODEL")
        or os.environ.get("LIVEKIT_TURN_DASHSCOPE_ASR_MODEL")
        or ""
    ).strip()
    if env:
        return env
    cfg = _livekit_turn_cfg()
    return str(cfg.get("dashscope_asr_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def dashscope_asr_ws_url() -> str:
    env = (
        os.environ.get("DASHSCOPE_ASR_WS_URL")
        or os.environ.get("LIVEKIT_TURN_DASHSCOPE_WS_URL")
        or ""
    ).strip()
    if env:
        return env.rstrip("/")
    cfg = _livekit_turn_cfg()
    explicit = str(cfg.get("dashscope_ws_url") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    pipeline = _load_json().get("pipeline") or {}
    ws_id = str((pipeline or {}).get("workspace_id") or "").strip()
    if ws_id:
        return f"wss://{ws_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    return DEFAULT_WS


def dashscope_language_hints() -> list[str]:
    env = (os.environ.get("DASHSCOPE_ASR_LANGUAGE_HINTS") or "").strip()
    if env:
        return [part.strip() for part in env.split(",") if part.strip()]
    cfg = _livekit_turn_cfg().get("dashscope_language_hints")
    if isinstance(cfg, list):
        hints = [str(x).strip() for x in cfg if str(x).strip()]
        if hints:
            return hints
    return ["zh"]


class DashScopeAsrStreamingSession:
    """Async wrapper around dashscope.audio.asr.Recognition streaming API."""

    def __init__(
        self,
        *,
        model: str | None = None,
        language_hints: list[str] | None = None,
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_notify: Callable[[str, str], Awaitable[None]] | None = None,
        on_server_error: Callable[[int | None], Awaitable[None]] | None = None,
    ) -> None:
        self.model = (model or dashscope_asr_model()).strip()
        self.language_hints = list(language_hints or dashscope_language_hints())
        self.on_partial = on_partial
        self.on_notify = on_notify
        self.on_server_error = on_server_error
        self._recognition = None
        self._final_text = ""
        self._done = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aborted = False

    async def start(self) -> None:
        self._aborted = False
        self._final_text = ""
        self._done.clear()
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> None:
        try:
            import dashscope
            from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
        except ImportError as exc:
            raise RuntimeError(
                "DashScope ASR unavailable: pip install dashscope>=1.20.0"
            ) from exc

        api_key = dashscope_asr_api_key()
        if not api_key:
            raise RuntimeError(
                "DashScope ASR needs DASHSCOPE_API_KEY (or followup.bailian.api_key in config.json)"
            )

        dashscope.api_key = api_key
        ws_url = dashscope_asr_ws_url()
        if ws_url:
            dashscope.base_websocket_api_url = ws_url

        session = self
        RecognitionResultCls = RecognitionResult

        class _Callback(RecognitionCallback):
            def on_event(self, result: RecognitionResult) -> None:
                try:
                    sentence = result.get_sentence()
                    if not isinstance(sentence, dict):
                        return
                    text = str(sentence.get("text") or "").strip()
                    if not text:
                        return
                    is_interim = not RecognitionResultCls.is_sentence_end(sentence)
                    session._schedule_partial(text, is_interim)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("DashScope ASR on_event failed: %s", exc)

            def on_complete(self) -> None:
                session._schedule_done()

            def on_error(self, message) -> None:
                err = getattr(message, "message", None) or str(message)
                LOG.warning("DashScope ASR error: %s", err)
                session._schedule_error(err)

        self._recognition = Recognition(
            model=self.model,
            format="pcm",
            sample_rate=16000,
            language_hints=self.language_hints,
            semantic_punctuation_enabled=False,
            callback=_Callback(),
        )
        self._recognition.start()
        LOG.info(
            "DashScope ASR started model=%s ws=%s hints=%s",
            self.model,
            ws_url,
            self.language_hints,
        )

    def _schedule_partial(self, text: str, is_interim: bool) -> None:
        self._final_text = text
        if not self._loop or not self.on_partial:
            return

        async def _run() -> None:
            await self.on_partial(text, is_interim)

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def _schedule_done(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._done.set)

    def _schedule_error(self, message: str) -> None:
        if self._loop and self.on_notify:

            async def _run() -> None:
                await self.on_notify("warning", f"DashScope ASR error: {message}")

            asyncio.run_coroutine_threadsafe(_run(), self._loop)
        if self._loop and self.on_server_error:

            async def _run_err() -> None:
                await self.on_server_error(None)

            asyncio.run_coroutine_threadsafe(_run_err(), self._loop)
        self._schedule_done()

    async def send_audio(self, pcm_chunk: bytes, is_last: bool = False) -> None:
        if not self._recognition:
            return
        if pcm_chunk:
            await asyncio.to_thread(self._recognition.send_audio_frame, pcm_chunk)
        if is_last and not self._aborted:
            await asyncio.to_thread(self._recognition.stop)

    async def send_keepalive(self) -> None:
        return

    async def abort(self) -> None:
        self._aborted = True
        recognition = self._recognition
        self._recognition = None
        if recognition is not None:
            try:
                await asyncio.to_thread(recognition.stop)
            except Exception:
                pass
        self._done.set()

    async def wait_final(self, timeout: float = 15.0) -> str:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._recognition = None
        return self._final_text
