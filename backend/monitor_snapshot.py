# -*- coding: utf-8 -*-
"""Shared helpers for t2sbg / omni monitor snapshot payloads."""
from __future__ import annotations

import base64
from datetime import datetime, timezone


def pcm16le_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    data_len = len(pcm_bytes)
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    riff_size = 36 + data_len
    header = (
        b"RIFF"
        + riff_size.to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + data_len.to_bytes(4, "little")
    )
    return header + pcm_bytes


def build_monitor_snapshot(
    *,
    conn_id: str,
    vad_merged: bytes,
    asr_merged: bytes,
    vad_turn_history: list[bytes],
    asr_turn_history: list[bytes],
    vad_segments: int,
    asr_segments: int,
    transcript_html: str,
    log_html: str,
    full_merged_wav_b64: str,
    full_turn_wavs_b64: list[str],
    source_mode: str = "t2s",
) -> dict[str, object]:
    vad_wav_b64 = (
        base64.b64encode(pcm16le_to_wav_bytes(vad_merged)).decode("ascii") if vad_merged else ""
    )
    asr_wav_b64 = (
        base64.b64encode(pcm16le_to_wav_bytes(asr_merged)).decode("ascii") if asr_merged else ""
    )
    vad_turn_b64 = [
        base64.b64encode(pcm16le_to_wav_bytes(seg)).decode("ascii") for seg in vad_turn_history if seg
    ]
    asr_turn_b64 = [
        base64.b64encode(pcm16le_to_wav_bytes(seg)).decode("ascii") for seg in asr_turn_history if seg
    ]
    published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "type": "monitor_snapshot",
        "vad_wav_base64": vad_wav_b64,
        "asr_wav_base64": asr_wav_b64,
        "vad_turn_wavs_base64": vad_turn_b64,
        "asr_turn_wavs_base64": asr_turn_b64,
        "vad_segments": vad_segments,
        "asr_segments": asr_segments,
        "vad_duration_sec": round(len(vad_merged) / 2 / 16000, 3),
        "asr_duration_sec": round(len(asr_merged) / 2 / 16000, 3),
        "transcript_html": transcript_html,
        "log_html": log_html,
        "full_merged_wav_base64": full_merged_wav_b64,
        "full_turn_wavs_base64": full_turn_wavs_b64,
        "published_at": published_at,
        "source_conn_id": conn_id,
        "source_mode": source_mode,
    }
