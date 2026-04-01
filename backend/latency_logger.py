# -*- coding: utf-8 -*-
"""
Latency measurement: log timestamps for user_speech_end, asr_complete,
llm_response_start, tts_start, audio_playback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

LOG = logging.getLogger(__name__)


@dataclass
class LatencyPoint:
    name: str
    ts: float
    question_id: str = ""
    reply_id: str = ""


class LatencyLogger:
    """Tracks timestamps per turn and computes total_latency = tts_start - user_speech_end."""

    def __init__(self):
        self._points: list[LatencyPoint] = []
        self._current_question_id: str = ""
        self._user_speech_end_ts: float | None = None
        self._tts_start_ts: float | None = None

    def user_speech_end(self, question_id: str = "") -> None:
        t = time.perf_counter()
        self._user_speech_end_ts = t
        self._current_question_id = question_id
        self._points.append(LatencyPoint("user_speech_end", t, question_id=question_id))
        LOG.debug("user_speech_end question_id=%s ts=%.3f", question_id, t)

    def asr_complete(self, question_id: str = "") -> None:
        t = time.perf_counter()
        self._points.append(LatencyPoint("asr_complete", t, question_id=question_id))
        LOG.debug("asr_complete question_id=%s ts=%.3f", question_id, t)

    def llm_response_start(self, question_id: str = "", reply_id: str = "") -> None:
        t = time.perf_counter()
        self._points.append(
            LatencyPoint("llm_response_start", t, question_id=question_id, reply_id=reply_id)
        )
        LOG.debug("llm_response_start question_id=%s reply_id=%s ts=%.3f", question_id, reply_id, t)

    def tts_start(self, question_id: str = "", reply_id: str = "") -> None:
        t = time.perf_counter()
        self._tts_start_ts = t
        self._points.append(
            LatencyPoint("tts_start", t, question_id=question_id, reply_id=reply_id)
        )
        if self._user_speech_end_ts is not None:
            total_ms = (t - self._user_speech_end_ts) * 1000
            LOG.info("latency tts_start - user_speech_end = %.0f ms (question_id=%s)", total_ms, question_id)
        LOG.debug("tts_start question_id=%s reply_id=%s ts=%.3f", question_id, reply_id, t)

    def audio_playback(self, question_id: str = "", reply_id: str = "") -> None:
        t = time.perf_counter()
        self._points.append(
            LatencyPoint("audio_playback", t, question_id=question_id, reply_id=reply_id)
        )
        LOG.debug("audio_playback question_id=%s reply_id=%s ts=%.3f", question_id, reply_id, t)

    def get_last_total_latency_ms(self) -> float | None:
        """Return (tts_start - user_speech_end) in ms for the last turn."""
        if self._user_speech_end_ts is None or self._tts_start_ts is None:
            return None
        return (self._tts_start_ts - self._user_speech_end_ts) * 1000

    def clear(self) -> None:
        self._points.clear()
        self._user_speech_end_ts = None
        self._tts_start_ts = None
        self._current_question_id = ""
