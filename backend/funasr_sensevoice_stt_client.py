# -*- coding: utf-8 -*-
"""Local CPU streaming STT via FunASR SenseVoiceSmall."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Callable

import numpy as np

LOG = logging.getLogger(__name__)

CHUNK_MS = 200
CHUNK_BYTES = (16000 * CHUNK_MS // 1000) * 2
_DEFAULT_MODEL_MS = "iic/SenseVoiceSmall"
_DEFAULT_MODEL_HF = "FunAudioLLM/SenseVoiceSmall"

_model = None
_model_lock = threading.Lock()


def _load_cfg() -> dict:
    try:
        from pathlib import Path
        import json

        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            block = (data.get("vad") or {}).get("livekit_turn") or {}
            if isinstance(block, dict):
                return block
    except Exception:
        pass
    return {}


def funasr_hub() -> str:
    env = (os.environ.get("FUNASR_HUB", "") or os.environ.get("LIVEKIT_TURN_FUNASR_HUB", "") or "").strip().lower()
    if env in {"hf", "huggingface"}:
        return "hf"
    if env in {"ms", "modelscope"}:
        return "ms"
    cfg = str(_load_cfg().get("funasr_hub", "") or "").strip().lower()
    if cfg in {"hf", "huggingface"}:
        return "hf"
    return "ms"


def funasr_sensevoice_model() -> str:
    env = (os.environ.get("FUNASR_MODEL", "") or os.environ.get("LIVEKIT_TURN_FUNASR_MODEL", "") or "").strip()
    if env:
        return env
    cfg = str(_load_cfg().get("funasr_model", "") or "").strip()
    if cfg:
        return cfg
    return _DEFAULT_MODEL_HF if funasr_hub() == "hf" else _DEFAULT_MODEL_MS


def funasr_sensevoice_language() -> str:
    env = (os.environ.get("FUNASR_LANGUAGE", "") or os.environ.get("LIVEKIT_TURN_FUNASR_LANGUAGE", "") or "").strip()
    if env:
        return env
    cfg = str(_load_cfg().get("funasr_language", "") or "").strip()
    if cfg:
        return cfg
    lk = (os.environ.get("LIVEKIT_TURN_LANGUAGE", "") or "").strip()
    if lk:
        return lk
    cfg_lk = str(_load_cfg().get("language", "") or "").strip()
    return cfg_lk or "zh"


def funasr_device() -> str:
    env = (os.environ.get("FUNASR_DEVICE", "") or os.environ.get("LIVEKIT_TURN_FUNASR_DEVICE", "") or "").strip()
    if env:
        return env
    cfg = str(_load_cfg().get("funasr_device", "") or "").strip()
    return cfg or "cpu"


def funasr_partial_interval_sec() -> float:
    env = (os.environ.get("FUNASR_PARTIAL_INTERVAL_SEC", "") or "").strip()
    if env:
        try:
            return max(0.15, float(env))
        except ValueError:
            pass
    cfg = _load_cfg().get("funasr_partial_interval_sec")
    if cfg is not None:
        try:
            return max(0.15, float(cfg))
        except (TypeError, ValueError):
            pass
    return 0.45


def _map_language(lang: str) -> str:
    cleaned = (lang or "zh").strip().lower()
    if cleaned in {"multi", "auto", "any"}:
        return "auto"
    if cleaned.startswith("zh"):
        return "zh"
    if cleaned.startswith("en"):
        return "en"
    if cleaned.startswith("yue") or cleaned in {"cantonese", "粤语"}:
        return "yue"
    if cleaned.startswith("ja"):
        return "ja"
    if cleaned.startswith("ko"):
        return "ko"
    return cleaned or "auto"


def _ensure_model_loaded():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from funasr import AutoModel
        except ImportError as exc:
            missing = str(exc).strip() or "unknown import error"
            raise RuntimeError(
                "FunASR SenseVoice STT unavailable: "
                f"{missing}. Install: pip install funasr modelscope torch torchaudio "
                "(CPU: pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu)"
            ) from exc

        model_id = funasr_sensevoice_model()
        hub = funasr_hub()
        device = funasr_device()
        kwargs: dict = {"model": model_id, "device": device}
        if hub == "hf":
            kwargs["hub"] = "hf"
        LOG.info("Loading FunASR SenseVoice model=%s hub=%s device=%s", model_id, hub, device)
        _model = AutoModel(**kwargs)
        return _model


def _postprocess_text(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        return rich_transcription_postprocess(text).strip()
    except Exception:
        return text


def _transcribe_pcm(pcm_bytes: bytes) -> str:
    if not pcm_bytes:
        return ""
    model = _ensure_model_loaded()
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    lang = _map_language(funasr_sensevoice_language())
    seconds = len(audio) / 16000.0
    batch_size_s = 300 if seconds > 30 else max(10, int(seconds) + 1)
    res = model.generate(
        input=audio,
        cache={},
        language=lang,
        use_itn=True,
        batch_size_s=batch_size_s,
    )
    if not res:
        return ""
    first = res[0]
    if isinstance(first, dict):
        raw = str(first.get("text", "") or "")
    else:
        raw = str(first)
    return _postprocess_text(raw)


class FunasrSenseVoiceStreamingSession:
    """Streaming wrapper: re-decode growing utterance buffer on CPU (SenseVoice)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        device: str | None = None,
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_notify: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.model = (model or funasr_sensevoice_model()).strip()
        self.language = (language or funasr_sensevoice_language()).strip()
        self.device = (device or funasr_device()).strip()
        self.on_partial = on_partial
        self.on_notify = on_notify
        self._pcm = bytearray()
        self._final_text = ""
        self._done = asyncio.Event()
        self._last_partial_mono = 0.0
        self._partial_interval = funasr_partial_interval_sec()
        self._transcribe_lock = asyncio.Lock()
        self._min_partial_bytes = CHUNK_BYTES

    async def start(self) -> None:
        self._pcm.clear()
        self._final_text = ""
        self._done.clear()
        self._last_partial_mono = 0.0
        try:
            await asyncio.to_thread(_ensure_model_loaded)
        except Exception as exc:
            if self.on_notify:
                await self.on_notify("error", f"FunASR SenseVoice load failed: {exc}")
            raise

    async def send_audio(self, pcm_chunk: bytes, is_last: bool = False) -> None:
        if pcm_chunk:
            self._pcm.extend(pcm_chunk)
            await self._maybe_partial()
        if is_last:
            await self._finalize()

    async def send_keepalive(self) -> None:
        return

    async def abort(self) -> None:
        self._pcm.clear()
        self._done.set()

    async def wait_final(self, timeout: float = 15.0) -> str:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._final_text

    async def _maybe_partial(self) -> None:
        now = time.monotonic()
        if now - self._last_partial_mono < self._partial_interval:
            return
        if len(self._pcm) < self._min_partial_bytes:
            return
        self._last_partial_mono = now
        async with self._transcribe_lock:
            try:
                text = await asyncio.to_thread(_transcribe_pcm, bytes(self._pcm))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("FunASR partial transcribe failed: %s", exc)
                return
            if not text or text == self._final_text:
                return
            self._final_text = text
            if self.on_partial:
                await self.on_partial(text, True)

    async def _finalize(self) -> None:
        async with self._transcribe_lock:
            try:
                text = await asyncio.to_thread(_transcribe_pcm, bytes(self._pcm))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("FunASR final transcribe failed: %s", exc)
                if self.on_notify:
                    await self.on_notify("warning", f"FunASR final transcribe failed: {exc}")
                text = self._final_text
            if text:
                self._final_text = text
                if self.on_partial:
                    await self.on_partial(text, False)
        self._done.set()
