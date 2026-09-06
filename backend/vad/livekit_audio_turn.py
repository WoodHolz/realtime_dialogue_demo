# -*- coding: utf-8 -*-
"""Silero VAD + LiveKit audio TurnDetector (v1 / v1-mini).

Unlike the text-based ``livekit_turn_detector`` (STT → EOU), this backend feeds
PCM directly into LiveKit Agents ``inference.TurnDetector`` — semantic + acoustic.
Default version is ``v1-mini`` (local CPU). Set version ``v1`` for LiveKit Inference
cloud when LIVEKIT_API_KEY + LIVEKIT_API_SECRET are present.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from backend.vad.config import (
    livekit_audio_max_endpointing_delay_secs,
    livekit_audio_min_endpointing_delay_secs,
    livekit_audio_turn_version,
    livekit_turn_language,
    resolve_livekit_audio_unlikely_threshold,
    smart_turn_silero_confidence,
    smart_turn_silero_stop_secs,
)
from backend.vad.silence_force import SilenceForceTracker
from backend.vad.types import VadEvent

LOG = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
# Docs: audio turn detector requires VAD min_silence >= 250ms; Silero default 0.55s.
_MIN_SILERO_STOP_SECS = 0.25


class LivekitAudioTurnBackend:
    """Silero (speech edges) + LiveKit audio TurnDetector (end-of-turn)."""

    provider = "livekit_turn_v1"

    def __init__(
        self,
        *,
        version: str | None = None,
        turn_threshold: float | str | None = None,
    ) -> None:
        stop_secs = max(_MIN_SILERO_STOP_SECS, smart_turn_silero_stop_secs())
        self._silero = SileroVADAnalyzer(
            sample_rate=_SAMPLE_RATE,
            params=VADParams(
                stop_secs=stop_secs,
                confidence=smart_turn_silero_confidence(),
            ),
        )
        self._version = (version or livekit_audio_turn_version()).strip().lower()
        if self._version not in {"v1", "v1-mini"}:
            self._version = "v1-mini"
        self._threshold_override = resolve_livekit_audio_unlikely_threshold(
            override=turn_threshold
        )
        self._language = livekit_turn_language()
        self._min_delay_secs = livekit_audio_min_endpointing_delay_secs()
        self._max_delay_secs = livekit_audio_max_endpointing_delay_secs()

        self._pcm_buf = bytearray()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force = SilenceForceTracker(self._max_delay_secs)
        self._quiet_elapsed_ms = 0.0
        self._pending_complete: bool | None = None
        self._pending_meta: dict[str, Any] = {}
        self._continue_emitted = False
        self._predict_task: asyncio.Task | None = None

        self._detector = None
        self._stream = None
        self._http_session = None
        self._lang_code = None
        self._resolved_threshold: float | None = None
        self._resolved_model = ""

    async def start_session(self, session_id: str) -> dict[str, Any]:
        del session_id
        await self._close_stream()
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force.reset()
        self._quiet_elapsed_ms = 0.0
        self._pending_complete = None
        self._pending_meta = {}
        self._continue_emitted = False
        self._silero.set_sample_rate(_SAMPLE_RATE)

        try:
            import aiohttp
            from livekit.agents import LanguageCode
            from livekit.agents.inference import TurnDetector
        except ImportError as exc:
            raise RuntimeError(
                "LiveKit audio TurnDetector requires livekit-agents>=1.6.1: "
                "pip install 'livekit-agents>=1.6.1'"
            ) from exc

        import os

        kwargs: dict[str, Any] = {
            "version": self._version,
            "sample_rate": _SAMPLE_RATE,
            "local_fallback": True,
        }
        if self._threshold_override is not None:
            kwargs["unlikely_threshold"] = float(self._threshold_override)

        # Outside LiveKit agent worker: must supply our own aiohttp session for cloud v1.
        api_key = (os.environ.get("LIVEKIT_API_KEY") or os.environ.get("LIVEKIT_INFERENCE_API_KEY") or "").strip()
        api_secret = (
            os.environ.get("LIVEKIT_API_SECRET") or os.environ.get("LIVEKIT_INFERENCE_API_SECRET") or ""
        ).strip()
        if self._version == "v1":
            if not api_key or not api_secret:
                raise RuntimeError(
                    "TurnDetector(version='v1') needs LIVEKIT_API_KEY + LIVEKIT_API_SECRET in .env"
                )
            kwargs["api_key"] = api_key
            kwargs["api_secret"] = api_secret
            inference_url = (os.environ.get("LIVEKIT_INFERENCE_URL") or "").strip()
            if inference_url:
                kwargs["base_url"] = inference_url
            self._http_session = aiohttp.ClientSession()
            kwargs["http_session"] = self._http_session

        self._detector = TurnDetector(**kwargs)
        self._stream = self._detector.stream()
        self._lang_code = LanguageCode(self._language)
        self._resolved_threshold = await self._stream.unlikely_threshold(self._lang_code)
        self._resolved_model = str(getattr(self._detector, "model", self._version))

        LOG.info(
            "LiveKit audio TurnDetector started version=%s model=%s lang=%s "
            "threshold=%s min_delay=%.2fs max_delay=%.2fs silero_stop=%.2fs http_session=%s",
            self._version,
            self._resolved_model,
            self._language,
            self._resolved_threshold,
            self._min_delay_secs,
            self._max_delay_secs,
            max(_MIN_SILERO_STOP_SECS, smart_turn_silero_stop_secs()),
            self._http_session is not None,
        )
        return {
            "provider": self.provider,
            "enabled": True,
            "mode": "local" if self._version == "v1-mini" else "cloud",
            "model": self._resolved_model,
            "version": self._version,
            "language": self._language,
            "threshold": self._resolved_threshold,
            "min_endpointing_delay_secs": self._min_delay_secs,
            "max_endpointing_delay_secs": self._max_delay_secs,
            "silence_force_secs": self._max_delay_secs,
        }

    def _frame_duration_ms(self, frame_bytes: int) -> float:
        return (frame_bytes / 2) / _SAMPLE_RATE * 1000.0

    def _push_audio(self, pcm: bytes) -> None:
        if self._stream is None or not pcm:
            return
        from livekit import rtc

        samples = len(pcm) // 2
        if samples <= 0:
            return
        frame = rtc.AudioFrame(pcm, _SAMPLE_RATE, 1, samples)
        try:
            self._stream.push_audio(frame)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("LiveKit audio TurnDetector push_audio failed: %s", exc)

    def _cancel_predict(self) -> None:
        task = self._predict_task
        self._predict_task = None
        if task and not task.done():
            task.cancel()
        if self._stream is not None:
            try:
                self._stream.cancel_inference()
            except Exception:
                pass
        self._pending_complete = None
        self._pending_meta = {}

    async def _run_predict(self) -> None:
        if self._stream is None:
            return
        try:
            fut = self._stream.predict()
            ev = await asyncio.wait_for(fut, timeout=5.0)
            prob = float(getattr(ev, "end_of_turn_probability", 0.0) or 0.0)
            threshold = self._resolved_threshold
            if threshold is None:
                threshold = await self._stream.unlikely_threshold(self._lang_code)
                self._resolved_threshold = threshold
            thr = float(threshold if threshold is not None else 0.355)
            # AgentSession: prob < unlikely_threshold → wait longer (not done).
            complete = prob >= thr
            meta = {
                "probability": round(prob, 4),
                "threshold": round(thr, 4),
                "language": self._language,
                "version": self._version,
                "model": self._resolved_model,
                "inference_duration": getattr(ev, "inference_duration", None),
            }
            self._pending_complete = complete
            self._pending_meta = meta
            LOG.debug(
                "LiveKit audio EOU predict complete=%s prob=%.4f thr=%.4f",
                complete,
                prob,
                thr,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.warning("LiveKit audio TurnDetector predict failed: %s", exc)
            self._pending_complete = None
            self._pending_meta = {"reason": "predict_failed", "error": str(exc)}

    def _schedule_predict(self) -> None:
        if self._predict_task and not self._predict_task.done():
            return
        self._predict_task = asyncio.create_task(self._run_predict())

    def _emit_turn_end(
        self,
        events: list[VadEvent],
        meta: dict[str, Any] | None = None,
        *,
        forced: bool = False,
    ) -> None:
        payload = dict(meta or {})
        if forced:
            payload.update(self._silence_force.force_meta())
            LOG.info(
                "LiveKit audio TurnDetector force turn end after %.0fms silence",
                self._silence_force.elapsed_ms,
            )
        events.append(
            VadEvent(
                kind="turn_end_candidate",
                provider=self.provider,
                state="speak",
                meta=payload,
            )
        )
        self._turn_active = False
        self._silence_force.reset()
        self._quiet_elapsed_ms = 0.0
        self._pending_complete = None
        self._pending_meta = {}
        self._continue_emitted = False
        self._cancel_predict()

    async def feed_pcm_s16le_16k(self, pcm_chunk: bytes) -> list[VadEvent]:
        if not pcm_chunk:
            return []

        events: list[VadEvent] = []
        self._pcm_buf.extend(pcm_chunk)
        frame_bytes = self._silero.num_frames_required() * 2

        while len(self._pcm_buf) >= frame_bytes:
            frame = bytes(self._pcm_buf[:frame_bytes])
            del self._pcm_buf[:frame_bytes]
            frame_ms = self._frame_duration_ms(frame_bytes)

            # Always feed audio EOU while a turn is active (or starting).
            if self._turn_active or self._prev_vad != VADState.QUIET:
                self._push_audio(frame)

            vad_state = await self._silero.analyze_audio(frame)
            is_speech = vad_state in (VADState.SPEAKING, VADState.STARTING)

            if vad_state == VADState.SPEAKING and self._prev_vad != VADState.SPEAKING:
                self._turn_active = True
                self._silence_force.reset()
                self._quiet_elapsed_ms = 0.0
                self._continue_emitted = False
                self._cancel_predict()
                self._push_audio(frame)
                events.append(
                    VadEvent(
                        kind="speech_start",
                        provider=self.provider,
                        state="nonidle",
                    )
                )

            if self._turn_active:
                if is_speech:
                    self._silence_force.reset()
                    self._quiet_elapsed_ms = 0.0
                    self._pending_complete = None
                    if self._predict_task and not self._predict_task.done():
                        self._cancel_predict()
                else:
                    self._silence_force.tick(frame_ms)
                    self._quiet_elapsed_ms += frame_ms
                    self._push_audio(frame)

            # Start EOU once Silero settles into quiet after speech.
            if (
                vad_state == VADState.QUIET
                and self._prev_vad in (VADState.STOPPING, VADState.SPEAKING)
                and self._turn_active
            ):
                self._schedule_predict()

            if self._turn_active and vad_state == VADState.QUIET:
                if self._pending_complete is True:
                    if self._quiet_elapsed_ms >= self._min_delay_secs * 1000.0:
                        self._emit_turn_end(events, self._pending_meta)
                    # else wait for min_endpointing_delay
                elif self._pending_complete is False:
                    if not self._continue_emitted:
                        self._continue_emitted = True
                        events.append(
                            VadEvent(
                                kind="speech_continue",
                                provider=self.provider,
                                state="nonidle",
                                meta=dict(self._pending_meta),
                            )
                        )
                    if self._silence_force.reached():
                        self._emit_turn_end(events, self._pending_meta, forced=True)
                elif self._silence_force.reached():
                    self._emit_turn_end(events, self._pending_meta or None, forced=True)

            self._prev_vad = vad_state

        return events

    async def _close_stream(self) -> None:
        self._cancel_predict()
        stream = self._stream
        self._stream = None
        self._detector = None
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:
                pass
        http = self._http_session
        self._http_session = None
        if http is not None and not http.closed:
            try:
                await http.close()
            except Exception:
                pass

    async def close_session(self) -> None:
        await self._close_stream()
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force.reset()
        self._quiet_elapsed_ms = 0.0
        try:
            await self._silero.cleanup()
        except Exception:
            pass
