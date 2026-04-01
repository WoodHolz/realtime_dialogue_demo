# -*- coding: utf-8 -*-
"""
Local ASR (speech-to-text) helper for t2s mode.

For demo purposes this uses Whisper via the `openai-whisper` Python package.
The audio format we record from the browser is 16kHz, mono, PCM S16LE.
"""
from __future__ import annotations

import io
import logging
from functools import lru_cache
from typing import Optional

import numpy as np

LOG = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_model():
    """Lazy-load Whisper model on first use."""
    try:
        import whisper  # type: ignore[import]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Whisper ASR model is not available. Please install it via "
            "`pip install openai-whisper` and make sure ffmpeg is installed."
        ) from exc

    # 使用 small / base 即可，权衡速度与效果；用户可自行调整。
    model_name = "small"
    LOG.info("Loading Whisper ASR model: %s", model_name)
    return whisper.load_model(model_name)


def transcribe_pcm16le_16k(pcm_bytes: bytes, language: str = "zh") -> str:
    """
    Transcribe a chunk of PCM S16LE 16kHz mono audio to text.

    This is intentionally简单，适合 demo；生产场景可以改成流式 / 分段识别。
    """
    if not pcm_bytes:
        return ""

    # Convert raw PCM to float32 waveform in [-1, 1].
    audio_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    try:
        model = _load_model()
    except RuntimeError as exc:
        LOG.warning("Local ASR unavailable: %s", exc)
        return ""

    try:
        # Whisper 会自动处理 16k → 16k/32k 内部采样；我们直接传 ndarray。
        result = model.transcribe(audio_np, language=language)
        text = (result.get("text") or "").strip()
        LOG.info("Local ASR result: %s", text)
        return text
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Local ASR failed: %s", exc)
        return ""

