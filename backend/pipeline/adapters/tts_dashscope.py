# -*- coding: utf-8 -*-
"""StreamingTts adapter over DashScope SpeechSynthesizer (qwen-audio TTS).

Bidirectional streaming: feed() → streaming_call, flush() → streaming_complete,
cancel() → streaming_cancel. PCM 24 kHz s16le matches the browser contract.

Notes:
- SDK default WS connect timeout is 5s; we pre-connect with a longer timeout on start().
- feed()/flush() never raise into the LLM stream (TimeoutError used to kill gen tasks).
- Server requires continue-task / streaming_call gaps < ~23s; leave sessions idle and they die.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import contextmanager

LOG = logging.getLogger(__name__)

AudioCb = Callable[[bytes], Awaitable[None] | None]
StartCb = Callable[[], Awaitable[None] | None]
EndCb = Callable[[], Awaitable[None] | None]

_CONNECT_TIMEOUT_SEC = float(os.getenv("PIPELINE_TTS_CONNECT_TIMEOUT", "20") or "20")
# Normal turn: wait for remaining synthesis after streaming_complete (opening can be long).
# Barge-in cancels the flush task + streaming_cancel; do not shorten this or mid-sentence cuts.
_FLUSH_TIMEOUT_MS = int(os.getenv("PIPELINE_TTS_FLUSH_TIMEOUT_MS", "60000") or "60000")


@contextmanager
def _prefer_ipv4():
    """DashScope WSS can hang on broken IPv6 routes; prefer AF_INET like omni_bridge."""
    prefer = (os.getenv("PIPELINE_TTS_WS_FAMILY") or os.getenv("OMNI_WS_FAMILY") or "inet4").strip().lower()
    if prefer not in {"inet4", "ipv4", "v4", "4"}:
        yield
        return
    real = socket.getaddrinfo

    def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        infos = real(host, port, family, type, proto, flags)
        v4 = [i for i in infos if i[0] == socket.AF_INET]
        return v4 + [i for i in infos if i[0] != socket.AF_INET] if v4 else infos

    socket.getaddrinfo = _ipv4_first  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real  # type: ignore[assignment]


class DashScopeSpeechTts:
    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str,
        model: str,
        voice: str,
        sample_rate: int = 24000,
        on_audio: AudioCb | None = None,
        on_start: StartCb | None = None,
        on_end: EndCb | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.ws_url = (ws_url or "").strip()
        self.model = (model or "").strip()
        self.default_voice = (voice or "").strip()
        self.sample_rate = int(sample_rate or 24000)
        self.on_audio = on_audio
        self.on_start = on_start
        self.on_end = on_end
        self._synth = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pcm_q: asyncio.Queue[bytes | None] | None = None
        self._pump: asyncio.Task | None = None
        self._cancelled = False
        self._started = False
        self._failed = False
        self._gen_id = ""
        self._voice = self.default_voice
        self._feed_lock = asyncio.Lock()

    def bind(
        self,
        *,
        on_audio: AudioCb | None = None,
        on_start: StartCb | None = None,
        on_end: EndCb | None = None,
    ) -> None:
        if on_audio:
            self.on_audio = on_audio
        if on_start:
            self.on_start = on_start
        if on_end:
            self.on_end = on_end

    async def start(self, *, voice: str, sample_rate: int = 24000, gen_id: str = "") -> None:
        if not self.api_key:
            raise RuntimeError("DashScopeSpeechTts needs DASHSCOPE_API_KEY")
        if not self.default_voice and not (voice or "").strip():
            raise RuntimeError("DashScopeSpeechTts needs PIPELINE_TTS_VOICE")
        await self.cancel()
        self._cancelled = False
        self._failed = False
        self._started = True
        self._gen_id = gen_id
        self._voice = (voice or "").strip() or self.default_voice
        if sample_rate:
            self.sample_rate = int(sample_rate)
        self._loop = asyncio.get_running_loop()
        self._pcm_q = asyncio.Queue()
        self._pump = asyncio.create_task(self._pump_audio())
        try:
            self._synth = await asyncio.to_thread(self._make_and_preconnect)
        except Exception:
            self._failed = True
            self._started = False
            self._synth = None
            await self._stop_pump(join=False)
            LOG.warning("DashScopeSpeechTts websocket preconnect failed gen_id=%s", gen_id, exc_info=True)
            raise
        if self.on_start:
            result = self.on_start()
            if asyncio.iscoroutine(result):
                await result

    async def feed(self, text: str) -> None:
        """Never raise: LLM on_delta must not die when TTS WS flakes."""
        if self._cancelled or self._failed or not self._started or not self._synth:
            return
        payload = (text or "").strip()
        if not payload:
            return
        async with self._feed_lock:
            if self._cancelled or self._failed or not self._synth:
                return
            try:
                await asyncio.to_thread(self._streaming_call_safe, payload)
            except Exception:
                self._failed = True
                LOG.warning(
                    "DashScopeSpeechTts feed failed gen_id=%s text_len=%s",
                    self._gen_id,
                    len(payload),
                    exc_info=True,
                )

    async def flush(self) -> None:
        """Finish the current synthesizer session and emit on_end when audio is done.

        Must not hold ``_feed_lock`` across ``streaming_complete``: barge-in needs
        to ``cancel()`` / ``start()`` without waiting for the full flush window.
        On barge-in the orchestrator cancels this task and ``_cancelled`` makes us
        exit without stopping a newer session.
        """
        gen = self._gen_id
        async with self._feed_lock:
            synth = self._synth
            if self._cancelled or not self._started or synth is None:
                return
        timeout_ms = max(1000, _FLUSH_TIMEOUT_MS)
        try:
            await asyncio.to_thread(synth.streaming_complete, timeout_ms)
        except TimeoutError as exc:
            if self._cancelled or self._gen_id != gen:
                LOG.debug("DashScopeSpeechTts flush timeout after cancel gen_id=%s", gen)
                return
            LOG.warning("DashScopeSpeechTts flush timeout gen_id=%s: %s", gen, exc)
        except Exception:
            if self._cancelled or self._gen_id != gen:
                return
            LOG.warning("DashScopeSpeechTts flush failed gen_id=%s", gen, exc_info=True)
            return
        if self._cancelled or self._gen_id != gen:
            return
        await self._stop_pump(join=True)
        if self._cancelled or self._gen_id != gen:
            return
        if self._synth is synth:
            self._synth = None
        self._started = False
        if self.on_end and not self._cancelled:
            result = self.on_end()
            if asyncio.iscoroutine(result):
                await result

    async def cancel(self) -> None:
        self._cancelled = True
        self._started = False
        synth = self._synth
        self._synth = None
        if synth is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(synth.streaming_cancel, 200), timeout=0.6)
            except Exception:
                LOG.debug("DashScopeSpeechTts cancel failed", exc_info=True)
                try:
                    synth.close()
                except Exception:
                    pass
        await self._stop_pump(join=False)

    def _make_and_preconnect(self):
        synth = self._make_synthesizer()
        self._preconnect(synth, timeout_sec=_CONNECT_TIMEOUT_SEC)
        return synth

    def _streaming_call_safe(self, payload: str) -> None:
        synth = self._synth
        if synth is None:
            return
        try:
            with _prefer_ipv4():
                synth.streaming_call(payload)
        except TimeoutError:
            # One rebuild+retry: SDK hardcodes 5s on first streaming_call if not connected.
            LOG.warning("DashScopeSpeechTts streaming_call timeout; rebuilding synthesizer once")
            self._discard_synth(synth)
            synth = self._make_synthesizer()
            self._synth = synth
            self._preconnect(synth, timeout_sec=_CONNECT_TIMEOUT_SEC)
            with _prefer_ipv4():
                synth.streaming_call(payload)

    def _discard_synth(self, synth) -> None:
        try:
            synth.close()
        except Exception:
            pass
        if self._synth is synth:
            self._synth = None

    def _preconnect(self, synth, *, timeout_sec: float) -> None:
        """SDK: establish WS before first text to cut TTFA / avoid 5s default race."""
        connect = getattr(synth, "_SpeechSynthesizer__connect", None)
        if not callable(connect):
            return
        with _prefer_ipv4():
            connect(float(timeout_sec))

    def _make_synthesizer(self):
        try:
            import dashscope
            from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer
        except ImportError as exc:
            raise RuntimeError("dashscope is required for pipeline TTS: pip install dashscope") from exc

        dashscope.api_key = self.api_key
        if self.ws_url:
            dashscope.base_websocket_api_url = self.ws_url

        fmt_name = f"PCM_{self.sample_rate}HZ_MONO_16BIT"
        audio_format = getattr(AudioFormat, fmt_name, None) or AudioFormat.PCM_24000HZ_MONO_16BIT
        adapter = self

        class _Callback(ResultCallback):
            def on_data(self, data: bytes) -> None:
                if adapter._cancelled or not data:
                    return
                loop = adapter._loop
                queue = adapter._pcm_q
                if loop is None or queue is None:
                    return
                loop.call_soon_threadsafe(queue.put_nowait, data)

            def on_error(self, message) -> None:
                LOG.warning("DashScopeSpeechTts error: %s", message)
                adapter._failed = True

        return SpeechSynthesizer(
            model=self.model,
            voice=self._voice,
            format=audio_format,
            callback=_Callback(),
            url=self.ws_url or None,
        )

    async def _pump_audio(self) -> None:
        assert self._pcm_q is not None
        try:
            while True:
                chunk = await self._pcm_q.get()
                if chunk is None:
                    return
                if self._cancelled:
                    continue
                if self.on_audio:
                    result = self.on_audio(chunk)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            return

    async def _stop_pump(self, *, join: bool) -> None:
        queue = self._pcm_q
        pump = self._pump
        if queue is not None:
            try:
                queue.put_nowait(None)
            except Exception:
                pass
        if pump and not pump.done():
            if join:
                try:
                    await asyncio.wait_for(pump, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pump.cancel()
            else:
                pump.cancel()
                try:
                    await pump
                except asyncio.CancelledError:
                    pass
        self._pump = None
        self._pcm_q = None
