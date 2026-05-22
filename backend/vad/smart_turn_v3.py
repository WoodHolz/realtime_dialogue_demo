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
)
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

    async def start_session(self, session_id: str) -> dict[str, Any]:
        del session_id  # stateless per websocket turn; reset analyzers each start
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        self._silero.set_sample_rate(_SAMPLE_RATE)
        self._turn.set_sample_rate(_SAMPLE_RATE)
        self._turn.clear()
        LOG.info("Smart Turn v3 session started")
        return {
            "provider": self.provider,
            "enabled": True,
            "mode": "local",
            "model": "smart-turn-v3.2-cpu",
        }

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
            is_speech = vad_state in (VADState.SPEAKING, VADState.STARTING)
            self._turn.append_audio(frame, is_speech)

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
        self._pcm_buf.clear()
        self._prev_vad = VADState.QUIET
        self._turn_active = False
        try:
            await self._silero.cleanup()
        except Exception:
            pass
        try:
            await self._turn.cleanup()
        except Exception:
            pass
