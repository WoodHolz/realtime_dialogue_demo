# -*- coding: utf-8 -*-
"""Telephony audio: PCMU 8 kHz ↔ PCM16LE + resample for Voice Agent."""
from __future__ import annotations

import array
import logging
import math

LOG = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

try:
    import soxr
except Exception:  # noqa: BLE001
    soxr = None

# G.711 μ-law bias / clip
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635

TELEPHONY_RATE = 8000
AGENT_IN_RATE = 16000
AGENT_TTS_RATE = 24000
PCMU_FRAME_SAMPLES = 160  # 20 ms @ 8 kHz
PCMU_FRAME_BYTES = PCMU_FRAME_SAMPLES
PCMU_SILENCE = b"\xff" * PCMU_FRAME_BYTES


def _linear_to_ulaw_sample(sample: int) -> int:
    sample = int(sample)
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample = sample + _ULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_to_linear_sample(ulaw: int) -> int:
    ulaw = ~int(ulaw) & 0xFF
    sign = ulaw & 0x80
    exponent = (ulaw >> 4) & 0x07
    mantissa = ulaw & 0x0F
    sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
    sample -= _ULAW_BIAS
    return -sample if sign else sample


_ULAW_DECODE = array.array("h", (_ulaw_to_linear_sample(i) for i in range(256)))


def pcmu_to_pcm16(pcmu: bytes) -> bytes:
    """Decode μ-law bytes → PCM16LE mono."""
    if not pcmu:
        return b""
    out = array.array("h", (_ULAW_DECODE[b] for b in pcmu))
    return out.tobytes()


def pcm16_to_pcmu(pcm16: bytes) -> bytes:
    """Encode PCM16LE mono → μ-law bytes."""
    if not pcm16:
        return b""
    if len(pcm16) % 2:
        pcm16 = pcm16[:-1]
    samples = array.array("h")
    samples.frombytes(pcm16)
    return bytes(_linear_to_ulaw_sample(s) for s in samples)


def _pcm16_to_floats(pcm16: bytes) -> list[float]:
    samples = array.array("h")
    samples.frombytes(pcm16)
    return [float(s) for s in samples]


def _floats_to_pcm16(values: list[float]) -> bytes:
    out = array.array("h")
    for v in values:
        iv = int(round(v))
        if iv > 32767:
            iv = 32767
        elif iv < -32768:
            iv = -32768
        out.append(iv)
    return out.tobytes()


def _resample_linear(pcm16: bytes, in_rate: int, out_rate: int) -> bytes:
    if not pcm16 or in_rate <= 0 or out_rate <= 0 or in_rate == out_rate:
        return pcm16
    if len(pcm16) % 2:
        pcm16 = pcm16[:-1]
    audio = _pcm16_to_floats(pcm16)
    if not audio:
        return b""
    out_len = max(1, int(round(len(audio) * (out_rate / in_rate))))
    if len(audio) == 1:
        return _floats_to_pcm16([audio[0]] * out_len)
    last = len(audio) - 1
    out: list[float] = []
    for i in range(out_len):
        src = i * last / (out_len - 1) if out_len > 1 else 0.0
        lo = int(math.floor(src))
        hi = min(lo + 1, last)
        frac = src - lo
        out.append(audio[lo] * (1.0 - frac) + audio[hi] * frac)
    return _floats_to_pcm16(out)


def resample_pcm16(pcm16: bytes, in_rate: int, out_rate: int) -> bytes:
    """Resample PCM16LE mono; prefers soxr/numpy when available."""
    if not pcm16 or in_rate == out_rate:
        return pcm16
    if len(pcm16) % 2:
        pcm16 = pcm16[:-1]
    if np is not None and soxr is not None:
        try:
            audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
            if audio.size == 0:
                return b""
            out = soxr.resample(audio, in_rate, out_rate).astype(np.float32)
            return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()
        except Exception:
            LOG.debug("soxr resample failed; falling back to linear", exc_info=True)
    if np is not None:
        try:
            audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
            if audio.size == 0:
                return b""
            out_len = max(1, int(round(audio.size * (out_rate / in_rate))))
            in_idx = np.arange(audio.size, dtype=np.float32)
            out_idx = np.linspace(0, max(0, audio.size - 1), out_len, dtype=np.float32)
            resampled = np.interp(out_idx, in_idx, audio)
            return np.clip(np.round(resampled), -32768, 32767).astype("<i2").tobytes()
        except Exception:
            LOG.debug("numpy resample failed; falling back to linear", exc_info=True)
    return _resample_linear(pcm16, in_rate, out_rate)


def telephony_rx_to_agent(pcmu_frame: bytes) -> bytes:
    """PCMU 8 kHz frame → PCM16LE 16 kHz for Duplex/Omni uplink."""
    pcm8 = pcmu_to_pcm16(pcmu_frame)
    return resample_pcm16(pcm8, TELEPHONY_RATE, AGENT_IN_RATE)


def agent_tts_to_telephony(pcm16_tts: bytes, *, tts_rate: int = AGENT_TTS_RATE) -> bytes:
    """Agent TTS PCM16 → PCMU 8 kHz (may be partial; caller should buffer to 160B)."""
    pcm8 = resample_pcm16(pcm16_tts, tts_rate, TELEPHONY_RATE)
    return pcm16_to_pcmu(pcm8)


class PcmuFrameBuffer:
    """Accumulate PCMU bytes and emit fixed 20 ms frames."""

    def __init__(self, frame_bytes: int = PCMU_FRAME_BYTES) -> None:
        self._frame = frame_bytes
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        if not data:
            return []
        self._buf.extend(data)
        frames: list[bytes] = []
        while len(self._buf) >= self._frame:
            frames.append(bytes(self._buf[: self._frame]))
            del self._buf[: self._frame]
        return frames

    def flush_pad_silence(self) -> bytes | None:
        if not self._buf:
            return None
        pad = self._frame - len(self._buf)
        frame = bytes(self._buf) + (b"\xff" * pad)
        self._buf.clear()
        return frame
