from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from backend.funasr_sensevoice_stt_client import CHUNK_BYTES as STT_CHUNK_BYTES
from backend.livekit_turn_stt import (
    create_livekit_turn_stt_session,
    livekit_turn_stt_backend,
    livekit_turn_stt_language_label,
    livekit_turn_stt_model_label,
)
from backend.vad.config import (
    livekit_turn_embedded_asr_enabled,
    livekit_turn_language,
    resolve_livekit_turn_threshold,
    smart_turn_silero_confidence,
    smart_turn_silero_stop_secs,
)
from backend.vad.livekit_eou_model import build_chat_ctx, get_livekit_eou_model
from backend.vad.types import VadEvent

LOG = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


class LivekitTurnDetectorBackend:
    """Silero VAD + local/cloud STT + LiveKit turn-detector semantic EOU."""

    provider = "livekit_turn_detector"

    def __init__(
        self,
        *,
        force_embedded_asr: bool | None = None,
        stt_backend: str | None = None,
        turn_threshold: float | str | None = None,
    ) -> None:
        stop_secs = smart_turn_silero_stop_secs()
        self._silero = SileroVADAnalyzer(
            sample_rate=_SAMPLE_RATE,
            params=VADParams(
                stop_secs=stop_secs,
                confidence=smart_turn_silero_confidence(),
            ),
        )
        self._eou = get_livekit_eou_model()
        self._language = livekit_turn_language()
        self._threshold_override = resolve_livekit_turn_threshold(override=turn_threshold)
        self._stt_backend = livekit_turn_stt_backend()
        if stt_backend is not None:
            from backend.livekit_turn_stt import resolve_livekit_turn_stt_backend

            self._stt_backend = resolve_livekit_turn_stt_backend(override=stt_backend)
        if force_embedded_asr is None:
            self._embedded_asr_enabled = livekit_turn_embedded_asr_enabled()
        else:
            self._embedded_asr_enabled = force_embedded_asr

        self._pcm_buf = bytearray()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._user_transcript = ""
        self._chat_history: list[dict[str, str]] = []
        self._external_transcript = False

        self._stt_session = None
        self._stt_buffer = bytearray()

    def update_user_transcript(self, text: str) -> None:
        """Feed partial/final ASR text from the outer pipeline (t2s mode)."""
        cleaned = (text or "").strip()
        if cleaned:
            self._user_transcript = cleaned
            self._external_transcript = True

    def update_chat_history(self, history: list[dict[str, str]]) -> None:
        """Optional multi-turn context for semantic EOU."""
        self._chat_history = list(history)

    async def start_session(self, session_id: str) -> dict[str, Any]:
        del session_id
        await self._stop_embedded_stt()
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._user_transcript = ""
        self._external_transcript = False
        self._stt_buffer.clear()
        self._silero.set_sample_rate(_SAMPLE_RATE)
        effective_threshold = self._eou.threshold_for_language(
            self._language,
            override=self._threshold_override,
        )
        LOG.info(
            "LiveKit turn-detector session started (eou_lang=%s threshold=%s stt=%s/%s/%s)",
            self._language,
            effective_threshold,
            self._stt_backend,
            livekit_turn_stt_model_label(self._stt_backend),
            livekit_turn_stt_language_label(self._stt_backend),
        )
        return {
            "provider": self.provider,
            "enabled": True,
            "mode": "local",
            "model": "livekit/turn-detector",
            "revision": "v0.4.1-intl",
            "language": self._language,
            "threshold": effective_threshold,
            "stt": self._stt_backend,
            "stt_model": livekit_turn_stt_model_label(self._stt_backend),
            "stt_language": livekit_turn_stt_language_label(self._stt_backend),
        }

    async def _start_embedded_stt_if_needed(self) -> None:
        if self._external_transcript or not self._embedded_asr_enabled:
            return
        if self._stt_session is not None:
            return

        async def _on_partial(text: str, is_interim: bool) -> None:
            del is_interim
            if text.strip():
                self._user_transcript = text.strip()

        session = create_livekit_turn_stt_session(on_partial=_on_partial, stt_backend=self._stt_backend)
        try:
            await session.start()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("LiveKit turn embedded STT start failed: %s", exc)
            return
        self._stt_session = session

    async def _feed_embedded_stt(self, pcm_chunk: bytes) -> None:
        if self._stt_session is None or not pcm_chunk:
            return
        self._stt_buffer.extend(pcm_chunk)
        while len(self._stt_buffer) >= STT_CHUNK_BYTES:
            chunk = bytes(self._stt_buffer[:STT_CHUNK_BYTES])
            del self._stt_buffer[:STT_CHUNK_BYTES]
            await self._stt_session.send_audio(chunk, is_last=False)

    async def _stop_embedded_stt(self) -> None:
        session = self._stt_session
        self._stt_session = None
        remainder = bytes(self._stt_buffer)
        self._stt_buffer.clear()
        if session is None:
            return
        try:
            await session.send_audio(remainder, is_last=True)
            await session.wait_final()
        except Exception:
            pass
        try:
            await session.abort()
        except Exception:
            pass

    async def _predict_turn_complete(self) -> tuple[bool, dict[str, Any]]:
        threshold = self._eou.threshold_for_language(
            self._language,
            override=self._threshold_override,
        )
        chat_ctx = build_chat_ctx(
            user_text=self._user_transcript,
            history=self._chat_history,
        )
        if not chat_ctx:
            return False, {"reason": "no_transcript", "threshold": threshold}

        prob, formatted = await asyncio.to_thread(
            self._eou.predict_eou_probability,
            chat_ctx,
        )
        meta: dict[str, Any] = {
            "probability": round(prob, 4),
            "threshold": round(threshold, 4),
            "language": self._language,
        }
        if formatted:
            meta["input_preview"] = formatted[-120:]
        return prob >= threshold, meta

    async def feed_pcm_s16le_16k(self, pcm_chunk: bytes) -> list[VadEvent]:
        if not pcm_chunk:
            return []

        events: list[VadEvent] = []
        self._pcm_buf.extend(pcm_chunk)
        frame_bytes = self._silero.num_frames_required() * 2

        while len(self._pcm_buf) >= frame_bytes:
            frame = bytes(self._pcm_buf[:frame_bytes])
            del self._pcm_buf[:frame_bytes]

            vad_state = await self._silero.analyze_audio(frame)

            if vad_state == VADState.SPEAKING and self._prev_vad != VADState.SPEAKING:
                self._turn_active = True
                self._user_transcript = ""
                self._external_transcript = False
                await self._start_embedded_stt_if_needed()
                events.append(
                    VadEvent(
                        kind="speech_start",
                        provider=self.provider,
                        state="nonidle",
                    )
                )

            if self._turn_active and vad_state in (
                VADState.SPEAKING,
                VADState.STARTING,
                VADState.STOPPING,
            ):
                await self._feed_embedded_stt(frame)

            if (
                vad_state == VADState.QUIET
                and self._prev_vad in (VADState.STOPPING, VADState.SPEAKING)
                and self._turn_active
            ):
                await self._feed_embedded_stt(frame)
                await self._stop_embedded_stt()

                complete, meta = await self._predict_turn_complete()
                if complete:
                    events.append(
                        VadEvent(
                            kind="turn_end_candidate",
                            provider=self.provider,
                            state="speak",
                            meta=meta,
                        )
                    )
                    self._turn_active = False
                else:
                    events.append(
                        VadEvent(
                            kind="speech_continue",
                            provider=self.provider,
                            state="nonidle",
                            meta=meta,
                        )
                    )

            self._prev_vad = vad_state

        return events

    async def close_session(self) -> None:
        await self._stop_embedded_stt()
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._user_transcript = ""
        try:
            await self._silero.cleanup()
        except Exception:
            pass
