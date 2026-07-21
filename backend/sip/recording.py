# -*- coding: utf-8 -*-
"""Call recording at the agent/telephony boundary.

Primary artifact: stereo WAV — left = user, right = AI.
Optional mono legs kept for analysis.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

LOG = logging.getLogger(__name__)


class CallRecorder:
    """
    Dual-channel recording (user | AI).

    - stereo.wav: PCM16LE stereo, L=user, R=AI (time-aligned, shorter side padded)
    - user.wav / ai.wav: optional mono legs (same sample rate)
    """

    def __init__(
        self,
        call_id: str,
        record_dir: str,
        *,
        user_rate: int = 16000,
        ai_rate: int = 16000,
    ) -> None:
        self.call_id = call_id
        self.user_rate = user_rate
        self.ai_rate = ai_rate
        self.root = Path(record_dir) / call_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._user = bytearray()
        self._ai = bytearray()
        self.paths = {
            "stereo": str(self.root / "stereo.wav"),
            "user": str(self.root / "user.wav"),
            "ai": str(self.root / "ai.wav"),
        }

    def append_user(self, pcm16: bytes) -> None:
        if pcm16:
            self._user.extend(pcm16)

    def append_ai(self, pcm16: bytes) -> None:
        if pcm16:
            self._ai.extend(pcm16)

    def close(self) -> dict[str, str]:
        try:
            user = bytes(self._user)
            ai = bytes(self._ai)
            rate = self.user_rate if self.user_rate == self.ai_rate else self.user_rate
            if self.user_rate != self.ai_rate:
                LOG.warning(
                    "record rates differ user=%s ai=%s call_id=%s; stereo uses user_rate",
                    self.user_rate,
                    self.ai_rate,
                    self.call_id,
                )
            self._write_mono_wav(self.paths["user"], user, self.user_rate)
            self._write_mono_wav(self.paths["ai"], ai, self.ai_rate)
            stereo = self._interleave_stereo(user, ai)
            self._write_stereo_wav(self.paths["stereo"], stereo, rate)
        except Exception:
            LOG.exception("failed to flush recordings call_id=%s", self.call_id)
        return dict(self.paths)

    @staticmethod
    def _write_mono_wav(path: str, pcm16: bytes, sample_rate: int) -> None:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16)

    @staticmethod
    def _write_stereo_wav(path: str, interleaved_pcm16: bytes, sample_rate: int) -> None:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(interleaved_pcm16)

    @staticmethod
    def _interleave_stereo(left: bytes, right: bytes) -> bytes:
        """PCM16LE: left=user, right=AI; pad the shorter side with silence."""
        n = max(len(left), len(right))
        if n % 2:
            n -= 1
        if n <= 0:
            return b""
        ll = left[:n].ljust(n, b"\x00")
        rr = right[:n].ljust(n, b"\x00")
        out = bytearray(n * 2)
        # For each 16-bit sample: L then R
        oi = 0
        for i in range(0, n, 2):
            out[oi : oi + 2] = ll[i : i + 2]
            out[oi + 2 : oi + 4] = rr[i : i + 2]
            oi += 4
        return bytes(out)
