# -*- coding: utf-8 -*-
"""In-call usage session + finalize helpers."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from backend.analytics.funnel import hangup_party_from_reason, infer_hangup_stage
from backend.analytics.models import CallAnalyticsRecord, UsageDelta
from backend.analytics.providers.doubao import DoubaoUsageAdapter
from backend.analytics.store import get_analytics_store

LOG = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallUsageSession:
    """Accumulate local + provider_session metrics for one call."""

    def __init__(
        self,
        call_id: str,
        *,
        scenario: str = "",
        provider: str = "doubao_dialog",
        model: str = "",
        input_audio_rate: int = 16000,
        output_audio_rate: int = 24000,
    ) -> None:
        self.call_id = call_id
        self.scenario = scenario
        self.provider = provider
        self.model = model
        self.input_audio_rate = max(1, int(input_audio_rate))
        self.output_audio_rate = max(1, int(output_audio_rate))
        self.started_at = _utcnow()
        self._t0 = time.perf_counter()
        self._audio_in_bytes = 0
        self._audio_out_bytes = 0
        self._provider_metrics: dict[str, float] = {}
        self._provider_extras: list[dict[str, Any]] = []
        self._adapter = DoubaoUsageAdapter()

    def add_input_pcm16(self, pcm: bytes, *, sample_rate: int | None = None) -> None:
        if not pcm:
            return
        rate = sample_rate or self.input_audio_rate
        # Normalize to session input rate byte-equivalent for consistent seconds.
        if rate != self.input_audio_rate and rate > 0:
            scale = self.input_audio_rate / float(rate)
            self._audio_in_bytes += int(len(pcm) * scale)
        else:
            self._audio_in_bytes += len(pcm)

    def add_output_pcm16(self, pcm: bytes, *, sample_rate: int | None = None) -> None:
        if not pcm:
            return
        rate = sample_rate or self.output_audio_rate
        if rate != self.output_audio_rate and rate > 0:
            scale = self.output_audio_rate / float(rate)
            self._audio_out_bytes += int(len(pcm) * scale)
        else:
            self._audio_out_bytes += len(pcm)

    def on_provider_event(self, event: dict[str, Any]) -> None:
        try:
            deltas: list[UsageDelta] = self._adapter.on_session_event(event)
        except Exception:
            LOG.debug("provider event parse failed", exc_info=True)
            return
        for delta in deltas:
            for k, v in (delta.metrics or {}).items():
                self._provider_metrics[k] = self._provider_metrics.get(k, 0.0) + float(v)
            if delta.extras:
                self._provider_extras.append(
                    {"event_type": delta.event_type, **delta.extras}
                )

    def local_usage_from_transcript(
        self,
        transcript: list[dict[str, Any]] | None,
    ) -> dict[str, float]:
        asr_chars = 0
        tts_chars = 0
        turns_user = 0
        turns_assistant = 0
        for row in transcript or []:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "").lower()
            text = str(row.get("text") or "")
            if role in {"user", "human"}:
                turns_user += 1
                asr_chars += len(text.strip())
            elif role in {"assistant", "ai", "bot"}:
                turns_assistant += 1
                tts_chars += len(text.strip())
        audio_in_sec = self._audio_in_bytes / float(self.input_audio_rate * 2)
        audio_out_sec = self._audio_out_bytes / float(self.output_audio_rate * 2)
        return {
            "audio_in_sec": round(audio_in_sec, 3),
            "audio_out_sec": round(audio_out_sec, 3),
            "asr_chars": float(asr_chars),
            "tts_chars": float(tts_chars),
            "turns_user": float(turns_user),
            "turns_assistant": float(turns_assistant),
        }

    def wall_duration_sec(self) -> float:
        return max(0.0, time.perf_counter() - self._t0)

    def build_record(
        self,
        *,
        connected: bool,
        hangup_reason: str = "",
        business_result: str = "",
        transcript: list[dict[str, Any]] | None = None,
        duration_sec: float | None = None,
        ended_at: str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> CallAnalyticsRecord:
        duration = float(duration_sec) if duration_sec is not None else self.wall_duration_sec()
        if not connected:
            duration = 0.0
        stage, confidence = infer_hangup_stage(
            scenario=self.scenario,
            connected=connected,
            hangup_reason=hangup_reason,
            business_result=business_result,
            transcript=transcript,
        )
        usage: dict[str, Any] = {
            "local": self.local_usage_from_transcript(transcript),
        }
        if self._provider_metrics or self._provider_extras:
            usage["provider_session"] = {
                **{k: round(v, 4) for k, v in self._provider_metrics.items()},
                **(
                    {"extras": self._provider_extras[-20:]}
                    if self._provider_extras
                    else {}
                ),
            }
        return CallAnalyticsRecord(
            call_id=self.call_id,
            scenario=self.scenario,
            provider=self.provider,
            model=self.model,
            started_at=self.started_at,
            ended_at=ended_at or _utcnow(),
            duration_sec=round(duration, 3),
            connected=bool(connected),
            hangup_reason=hangup_reason or "",
            hangup_party=hangup_party_from_reason(hangup_reason),
            hangup_stage=stage,
            hangup_stage_confidence=confidence,
            business_result=business_result or "",
            usage=usage,
            extras=dict(extras or {}),
        )


def record_call_analytics(record: CallAnalyticsRecord) -> CallAnalyticsRecord | None:
    try:
        return get_analytics_store().put(record)
    except Exception:
        LOG.exception("record_call_analytics failed call_id=%s", record.call_id)
        return None


def record_component_usage(
    call_id: str,
    component: str,
    metrics: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> None:
    if not call_id or not metrics:
        return
    payload = dict(metrics)
    if provider:
        payload.setdefault("provider", provider)
    if model:
        payload.setdefault("model", model)
    try:
        get_analytics_store().merge_component(call_id, component, payload)
    except Exception:
        LOG.exception(
            "record_component_usage failed call_id=%s component=%s",
            call_id,
            component,
        )
