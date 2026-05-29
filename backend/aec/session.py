# -*- coding: utf-8 -*-
"""
Per-connection server-side AEC using NLMS adaptive filtering.

WebRTC APM Python bindings are heavy to ship; this module provides a lightweight
fallback that taps TTS reference (far-end) and cleans uplink mic (near-end) before VAD.
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

from backend.aec.config import server_aec_delay_ms, server_aec_enabled

LOG = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000
TTS_REFERENCE_SAMPLE_RATE = 24000


def _resample_pcm_s16le(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if not pcm or src_rate == dst_rate:
        return pcm
    try:
        import soxr

        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        out = soxr.resample(arr, src_rate, dst_rate, quality="HQ")
        out_i16 = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return out_i16.tobytes()
    except Exception:
        # Nearest-neighbor fallback
        arr = np.frombuffer(pcm, dtype=np.int16)
        if len(arr) == 0:
            return b""
        ratio = dst_rate / float(src_rate)
        idx = (np.arange(int(len(arr) * ratio)) / ratio).astype(np.int64)
        idx = np.clip(idx, 0, len(arr) - 1)
        return arr[idx].tobytes()


class _NlmsAec:
    """Block NLMS echo canceller (mono 16 kHz)."""

    def __init__(self, *, filter_length: int = 2048, delay_samples: int = 0, mu: float = 0.25, eps: float = 1e-6):
        self.filter_length = max(64, int(filter_length))
        self.delay_samples = max(0, int(delay_samples))
        self.mu = float(mu)
        self.eps = float(eps)
        self._ref_ring: deque[float] = deque(maxlen=self.filter_length + self.delay_samples + 4096)
        self._weights = np.zeros(self.filter_length, dtype=np.float32)

    def feed_reference(self, samples: np.ndarray) -> None:
        for x in samples:
            self._ref_ring.append(float(x))

    def process(self, near: np.ndarray) -> np.ndarray:
        out = np.empty_like(near, dtype=np.float32)
        for i, d in enumerate(near):
            ref = self._ref_vector()
            y = float(np.dot(self._weights, ref))
            e = float(d) - y
            out[i] = e
            norm = float(np.dot(ref, ref)) + self.eps
            self._weights += (self.mu / norm) * e * ref
        return out

    def _ref_vector(self) -> np.ndarray:
        n = len(self._ref_ring)
        if n < self.delay_samples + self.filter_length:
            return np.zeros(self.filter_length, dtype=np.float32)
        start = n - self.delay_samples - self.filter_length
        vec = np.array(list(self._ref_ring)[start : start + self.filter_length], dtype=np.float32)
        if len(vec) < self.filter_length:
            pad = np.zeros(self.filter_length - len(vec), dtype=np.float32)
            vec = np.concatenate([pad, vec])
        return vec


class AecSession:
    """One AEC state machine per WebSocket connection."""

    def __init__(self, *, enabled: bool | None = None, sample_rate: int = TARGET_SAMPLE_RATE):
        self.sample_rate = int(sample_rate)
        self.enabled = server_aec_enabled() if enabled is None else bool(enabled)
        delay = server_aec_delay_ms()
        delay_samples = int(self.sample_rate * delay / 1000.0)
        self._engine = _NlmsAec(filter_length=2048, delay_samples=delay_samples)
        self._frames_in = 0
        self._frames_out = 0
        if self.enabled:
            LOG.info(
                "AecSession enabled sr=%s delay_ms=%s delay_samples=%s",
                self.sample_rate,
                delay,
                delay_samples,
            )

    def feed_reference_pcm(
        self,
        pcm_s16le: bytes,
        *,
        sample_rate: int = TARGET_SAMPLE_RATE,
    ) -> None:
        if not self.enabled or not pcm_s16le:
            return
        if sample_rate != self.sample_rate:
            pcm_s16le = _resample_pcm_s16le(pcm_s16le, sample_rate, self.sample_rate)
        ref = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        self._engine.feed_reference(ref)

    def feed_tts_reference_pcm(self, pcm_s16le: bytes) -> None:
        """TTS from Doubao/Omni is typically 24 kHz PCM."""
        self.feed_reference_pcm(pcm_s16le, sample_rate=TTS_REFERENCE_SAMPLE_RATE)

    def process_capture(self, pcm_s16le: bytes) -> bytes:
        if not pcm_s16le:
            return pcm_s16le
        if not self.enabled:
            return pcm_s16le
        near = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        cleaned = self._engine.process(near)
        out_i16 = np.clip(cleaned * 32768.0, -32768, 32767).astype(np.int16)
        self._frames_in += len(near)
        self._frames_out += len(out_i16)
        return out_i16.tobytes()

    def reset(self) -> None:
        delay = server_aec_delay_ms()
        delay_samples = int(self.sample_rate * delay / 1000.0)
        self._engine = _NlmsAec(filter_length=2048, delay_samples=delay_samples)


def create_aec_session(**kwargs: object) -> AecSession:
    return AecSession(**kwargs)
