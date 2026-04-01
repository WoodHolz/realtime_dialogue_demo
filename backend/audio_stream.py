# -*- coding: utf-8 -*-
"""
Audio stream utilities: PCM 16k mono s16le for Doubao API.
20ms per chunk = 320 samples = 640 bytes at 16kHz s16le.
"""
from __future__ import annotations

import struct

# Doubao: 16000 Hz, mono, int16, little-endian
SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
# 20ms chunks (recommended by API)
MS_PER_CHUNK = 20
SAMPLES_PER_CHUNK = SAMPLE_RATE * MS_PER_CHUNK // 1000
CHUNK_BYTES = SAMPLES_PER_CHUNK * BYTES_PER_SAMPLE  # 640


def pcm_chunks_from_bytes(data: bytes) -> list[bytes]:
    """Split raw PCM (s16le, 16k, mono) into 20ms chunks."""
    chunks = []
    offset = 0
    while offset + CHUNK_BYTES <= len(data):
        chunks.append(data[offset : offset + CHUNK_BYTES])
        offset += CHUNK_BYTES
    if offset < len(data):
        chunks.append(data[offset:])
    return chunks


def pcm_chunks_from_file(path: str) -> list[bytes]:
    """Read a raw PCM file (s16le, 16k, mono) and return 20ms chunks."""
    with open(path, "rb") as f:
        return pcm_chunks_from_bytes(f.read())


def resample_placeholder_trim(data: bytes, in_rate: int, in_channels: int = 1) -> bytes:
    """
    Placeholder: only supports 16k mono input.
    For other rates/channels you would use libsox or ffmpeg in production.
    """
    if in_rate == SAMPLE_RATE and in_channels == CHANNELS:
        return data
    # Simple downsample by dropping samples (e.g. 48k -> 16k: take every 3rd sample)
    if in_channels != 1:
        # take first channel only
        data = data[:: BYTES_PER_SAMPLE * in_channels]
    if in_rate == SAMPLE_RATE:
        return data
    ratio = in_rate / SAMPLE_RATE
    out_len = int(len(data) / (BYTES_PER_SAMPLE * in_channels) / ratio) * BYTES_PER_SAMPLE
    out = bytearray(out_len)
    for i in range(0, out_len, BYTES_PER_SAMPLE):
        src_i = int(i / BYTES_PER_SAMPLE * ratio) * BYTES_PER_SAMPLE
        if src_i + BYTES_PER_SAMPLE <= len(data):
            out[i : i + BYTES_PER_SAMPLE] = data[src_i : src_i + BYTES_PER_SAMPLE]
    return bytes(out)
