# -*- coding: utf-8 -*-
"""Call recording at the agent/telephony boundary.

Primary artifact: stereo WAV — left = user, right = AI.
Optional mono legs kept for analysis.
"""
from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path

LOG = logging.getLogger(__name__)


class CallRecorder:
    """
    Dual-channel recording (user | AI).

    - stereo.wav: PCM16LE stereo, L=user, R=AI (wall-clock aligned)
    - user.wav / ai.wav: optional mono legs (same sample rate)

    Timeline: both channels share one monotonic clock from mark_start() /
    first append. Gaps (AI between turns, user before uplink, packet loss)
    are filled with silence so L/R stay on the same axis.
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
        self._t0: float | None = None
        self._lock = threading.Lock()
        self.paths = {
            "stereo": str(self.root / "stereo.wav"),
            "user": str(self.root / "user.wav"),
            "ai": str(self.root / "ai.wav"),
        }

    def mark_start(self) -> None:
        """Anchor shared timeline (call answered). Idempotent."""
        with self._lock:
            if self._t0 is None:
                self._t0 = time.monotonic()

    def append_user(self, pcm16: bytes) -> None:
        self._append_aligned(self._user, pcm16, self.user_rate)

    def append_ai(self, pcm16: bytes) -> None:
        self._append_aligned(self._ai, pcm16, self.ai_rate)

    def _append_aligned(self, buf: bytearray, pcm16: bytes, rate: int) -> None:
        if not pcm16:
            return
        if len(pcm16) % 2:
            pcm16 = pcm16[:-1]
        if not pcm16:
            return
        with self._lock:
            now = time.monotonic()
            if self._t0 is None:
                self._t0 = now
            # Place chunk start at current wall clock; pad earlier gap with silence.
            target = int((now - self._t0) * rate) * 2
            if target > len(buf):
                buf.extend(b"\x00" * (target - len(buf)))
            buf.extend(pcm16)

    def close(self) -> dict[str, str]:
        try:
            with self._lock:
                now = time.monotonic()
                user = bytearray(self._user)
                ai = bytearray(self._ai)
                t0 = self._t0
            if t0 is not None:
                end_user = int((now - t0) * self.user_rate) * 2
                end_ai = int((now - t0) * self.ai_rate) * 2
                if end_user > len(user):
                    user.extend(b"\x00" * (end_user - len(user)))
                if end_ai > len(ai):
                    ai.extend(b"\x00" * (end_ai - len(ai)))
            user_b = bytes(user)
            ai_b = bytes(ai)
            rate = self.user_rate if self.user_rate == self.ai_rate else self.user_rate
            if self.user_rate != self.ai_rate:
                LOG.warning(
                    "record rates differ user=%s ai=%s call_id=%s; stereo uses user_rate",
                    self.user_rate,
                    self.ai_rate,
                    self.call_id,
                )
            self._write_mono_wav(self.paths["user"], user_b, self.user_rate)
            self._write_mono_wav(self.paths["ai"], ai_b, self.ai_rate)
            stereo = self._interleave_stereo(user_b, ai_b)
            self._write_stereo_wav(self.paths["stereo"], stereo, rate)
            LOG.info(
                "recording flushed call_id=%s user_bytes=%s ai_bytes=%s duration_approx=%.1fs",
                self.call_id,
                len(user_b),
                len(ai_b),
                (len(user_b) / 2) / float(self.user_rate) if self.user_rate else 0.0,
            )
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
