from __future__ import annotations

import logging
from typing import Any

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

from backend.vad.config import (
    smart_turn_cpu_count,
    smart_turn_model_path,
    smart_turn_silero_confidence,
    smart_turn_silero_stop_secs,
    vad_silence_force_secs,
)
from backend.vad.silence_force import SilenceForceTracker
from backend.vad.types import VadEvent

LOG = logging.getLogger(__name__)

_SAMPLE_RATE = 16000


class SmartTurnV3Backend:
    """Silero VAD + Pipecat LocalSmartTurnAnalyzerV3 (in-process)."""

    provider = "smart_turn_v3"

    def __init__(self) -> None:
        stop_secs = smart_turn_silero_stop_secs()
        self._silero = SileroVADAnalyzer(
            sample_rate=_SAMPLE_RATE,
            params=VADParams(
                stop_secs=stop_secs,
                confidence=smart_turn_silero_confidence(),
            ),
        )
        model_path = smart_turn_model_path()
        turn_kwargs: dict[str, Any] = {
            "sample_rate": _SAMPLE_RATE,
            "params": SmartTurnParams(stop_secs=stop_secs),
        }
        if model_path:
            turn_kwargs["smart_turn_model_path"] = model_path
            turn_kwargs["cpu_count"] = smart_turn_cpu_count()
        else:
            turn_kwargs["cpu_count"] = smart_turn_cpu_count()
        self._turn = LocalSmartTurnAnalyzerV3(**turn_kwargs)
        self._pcm_buf = bytearray()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force = SilenceForceTracker(vad_silence_force_secs())

    async def start_session(self, session_id: str) -> dict[str, Any]:
        del session_id  # stateless per websocket turn; reset analyzers each start
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force.reset()
        self._silero.set_sample_rate(_SAMPLE_RATE)
        self._turn.set_sample_rate(_SAMPLE_RATE)
        self._turn.clear()
        LOG.info(
            "Smart Turn v3 session started (silence_force_secs=%s)",
            vad_silence_force_secs(),
        )
        return {
            "provider": self.provider,
            "enabled": True,
            "mode": "local",
            "model": "smart-turn-v3.2-cpu",
            "silence_force_secs": vad_silence_force_secs(),
        }

    def _frame_duration_ms(self, frame_bytes: int) -> float:
        return (frame_bytes / 2) / _SAMPLE_RATE * 1000.0

    def _track_silence(self, *, is_speech: bool, frame_ms: float) -> None:
        if not self._turn_active:
            return
        if is_speech:
            self._silence_force.reset()
        else:
            self._silence_force.tick(frame_ms)

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
                "Smart Turn v3 force turn end after %.0fms silence (semantic rejected)",
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

            vad_state = await self._silero.analyze_audio(frame)
            is_speech = vad_state in (VADState.SPEAKING, VADState.STARTING)
            self._turn.append_audio(frame, is_speech)
            self._track_silence(is_speech=is_speech, frame_ms=frame_ms)

            if vad_state == VADState.SPEAKING and self._prev_vad != VADState.SPEAKING:
                self._turn.update_vad_start_secs(self._silero.params.start_secs)
                self._turn_active = True
                events.append(
                    VadEvent(
                        kind="speech_start",
                        provider=self.provider,
                        state="nonidle",
                    )
                )

            if (
                vad_state == VADState.QUIET
                and self._prev_vad in (VADState.STOPPING, VADState.SPEAKING)
                and self._turn_active
            ):
                eot, metrics = await self._turn.analyze_end_of_turn()
                meta: dict[str, Any] = {}
                if metrics is not None:
                    prob = getattr(metrics, "probability", None)
                    if prob is not None:
                        meta["probability"] = round(float(prob), 4)
                if eot == EndOfTurnState.COMPLETE:
                    self._emit_turn_end(events, meta)
                elif self._silence_force.reached():
                    self._emit_turn_end(events, meta, forced=True)
                else:
                    events.append(
                        VadEvent(
                            kind="speech_continue",
                            provider=self.provider,
                            state="nonidle",
                            meta=meta,
                        )
                    )
            elif (
                vad_state == VADState.QUIET
                and self._prev_vad == VADState.QUIET
                and self._turn_active
                and self._silence_force.reached()
            ):
                self._emit_turn_end(events, forced=True)

            self._prev_vad = vad_state

        return events

    async def close_session(self) -> None:
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silence_force.reset()
        try:
            await self._silero.cleanup()
        except Exception:
            pass
        try:
            await self._turn.cleanup()
        except Exception:
            pass
