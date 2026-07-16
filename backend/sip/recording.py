# -*- coding: utf-8 -*-
"""Simple multi-track PCM recording at the agent/telephony boundary."""
from __future__ import annotations

import logging
import wave
from pathlib import Path

LOG = logging.getLogger(__name__)


class CallRecorder:
    """
    Three-track tee (requirements: AI / user / mixed).

    Tracks are PCM16LE mono WAV files. Mix is a simple average of aligned bytes
    when both sides have data; otherwise the available leg is written.
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
            "user": str(self.root / "user.wav"),
            "ai": str(self.root / "ai.wav"),
            "mixed": str(self.root / "mixed.wav"),
        }

    def append_user(self, pcm16: bytes) -> None:
        if pcm16:
            self._user.extend(pcm16)

    def append_ai(self, pcm16: bytes) -> None:
        if pcm16:
            self._ai.extend(pcm16)

    def close(self) -> dict[str, str]:
        try:
            self._write_wav(self.paths["user"], bytes(self._user), self.user_rate)
            self._write_wav(self.paths["ai"], bytes(self._ai), self.ai_rate)
            mixed = self._mix(bytes(self._user), bytes(self._ai))
            # Prefer user rate for mixed when rates match; else write at user_rate length.
            self._write_wav(self.paths["mixed"], mixed, self.user_rate)
        except Exception:
            LOG.exception("failed to flush recordings call_id=%s", self.call_id)
        return dict(self.paths)

    @staticmethod
    def _write_wav(path: str, pcm16: bytes, sample_rate: int) -> None:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16)

    @staticmethod
    def _mix(a: bytes, b: bytes) -> bytes:
        if not a:
            return b
        if not b:
            return a
        # Align to 2-byte samples; pad shorter with zeros then average.
        n = max(len(a), len(b))
        if n % 2:
            n -= 1
        aa = a[:n].ljust(n, b"\x00")
        bb = b[:n].ljust(n, b"\x00")
        out = bytearray(n)
        for i in range(0, n, 2):
            sa = int.from_bytes(aa[i : i + 2], "little", signed=True)
            sb = int.from_bytes(bb[i : i + 2], "little", signed=True)
            mixed = max(-32768, min(32767, (sa + sb) // 2))
            out[i : i + 2] = int(mixed).to_bytes(2, "little", signed=True)
        return bytes(out)
