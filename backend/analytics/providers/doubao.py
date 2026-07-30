# -*- coding: utf-8 -*-
"""Doubao / Volcengine realtime dialog usage adapter."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from backend.analytics.models import ProviderDayUsage, UsageDelta

LOG = logging.getLogger(__name__)

PROVIDER_ID = "doubao_dialog"
DEFAULT_RESOURCE_ID = "volc.speech.dialog"


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _extract_usage_dict(event: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("usage", "token_usage", "billing", "quota_usage"):
        node = event.get(key)
        if isinstance(node, dict) and node:
            return node
    resp = event.get("response")
    if isinstance(resp, dict):
        node = resp.get("usage")
        if isinstance(node, dict) and node:
            return node
    return None


def _normalize_usage_metrics(raw: dict[str, Any]) -> dict[str, float]:
    """Map common vendor keys → standard analytics keys."""
    aliases = {
        "input_tokens": (
            "input_tokens",
            "prompt_tokens",
            "input_token",
            "text_input_tokens",
            "audio_input_tokens",
        ),
        "output_tokens": (
            "output_tokens",
            "completion_tokens",
            "output_token",
            "text_output_tokens",
            "audio_output_tokens",
        ),
        "total_tokens": ("total_tokens", "token", "tokens", "total_token"),
        "audio_in_sec": (
            "audio_in_sec",
            "input_audio_seconds",
            "audio_input_seconds",
            "asr_duration",
            "input_audio_duration",
        ),
        "audio_out_sec": (
            "audio_out_sec",
            "output_audio_seconds",
            "audio_output_seconds",
            "tts_duration",
            "output_audio_duration",
        ),
    }
    out: dict[str, float] = {}
    for std, keys in aliases.items():
        for k in keys:
            if k in raw:
                n = _as_float(raw[k])
                if n is not None:
                    out[std] = out.get(std, 0.0) + n
                    break
    # Sum nested input/output token parts if present.
    for group, std in (("input_token_details", "input_tokens"), ("output_token_details", "output_tokens")):
        details = raw.get(group)
        if isinstance(details, dict):
            for v in details.values():
                n = _as_float(v)
                if n is not None:
                    out[std] = out.get(std, 0.0) + n
    if "total_tokens" not in out:
        total = out.get("input_tokens", 0.0) + out.get("output_tokens", 0.0)
        if total:
            out["total_tokens"] = total
    return out


class DoubaoUsageAdapter:
    provider_id = PROVIDER_ID

    def on_session_event(self, event: dict[str, Any]) -> list[UsageDelta]:
        if not isinstance(event, dict):
            return []
        event_type = str(event.get("type") or "")
        usage_raw = _extract_usage_dict(event)
        # Also accept bare token fields on response.done-like events.
        if usage_raw is None and (
            "usage" in event_type
            or event_type.endswith(".done")
            or "token" in event_type.lower()
        ):
            if any(k in event for k in ("input_tokens", "output_tokens", "total_tokens", "tokens")):
                usage_raw = event
        if not usage_raw:
            return []
        metrics = _normalize_usage_metrics(usage_raw)
        if not metrics:
            # Keep raw for inspection even when keys are unfamiliar.
            return [
                UsageDelta(
                    metrics={},
                    extras={"raw_usage": usage_raw, "event_type": event_type},
                    event_type=event_type,
                )
            ]
        return [
            UsageDelta(
                metrics=metrics,
                extras={"raw_usage": usage_raw},
                event_type=event_type,
            )
        ]

    async def pull_day_usage(self, day: str) -> ProviderDayUsage:
        """
        Volcengine UsageMonitoring (account-level).

        Env:
          VOLC_ACCESS_KEY / VOLC_SECRET_KEY  (or VOLCENGINE_ACCESS_KEY / VOLCENGINE_SECRET_KEY)
          VOLC_USAGE_APP_ID (fallback DOUBAO_APP_ID)
          VOLC_USAGE_RESOURCE_ID (default volc.speech.dialog)
          VOLC_USAGE_ENABLED=1 to force attempt; without AK/SK → skipped
        """
        enabled = (os.environ.get("VOLC_USAGE_ENABLED") or "").strip().lower()
        ak = (
            (os.environ.get("VOLC_ACCESS_KEY") or "").strip()
            or (os.environ.get("VOLCENGINE_ACCESS_KEY") or "").strip()
            or (os.environ.get("VOLC_AK") or "").strip()
        )
        sk = (
            (os.environ.get("VOLC_SECRET_KEY") or "").strip()
            or (os.environ.get("VOLCENGINE_SECRET_KEY") or "").strip()
            or (os.environ.get("VOLC_SK") or "").strip()
        )
        app_id = (
            (os.environ.get("VOLC_USAGE_APP_ID") or "").strip()
            or (os.environ.get("DOUBAO_APP_ID") or "").strip()
        )
        resource_id = (
            (os.environ.get("VOLC_USAGE_RESOURCE_ID") or "").strip() or DEFAULT_RESOURCE_ID
        )

        if enabled in {"0", "false", "no", "off"}:
            return ProviderDayUsage(
                provider_id=self.provider_id,
                day=day,
                status="skipped",
                error="VOLC_USAGE_ENABLED=0",
            )
        if not ak or not sk or not app_id:
            return ProviderDayUsage(
                provider_id=self.provider_id,
                day=day,
                status="skipped",
                error="missing VOLC_ACCESS_KEY/VOLC_SECRET_KEY/VOLC_USAGE_APP_ID",
            )

        params = {
            "Action": "UsageMonitoring",
            "Version": "2023-11-07",
            "AppID": app_id,
            "ResourceID": resource_id,
            "Start": day,
            "End": day,
            "Mode": "daily",
        }
        try:
            # Prefer official SDK when available; else signed GET via volcengine OpenAPI helper.
            raw = await _volc_usage_monitoring_request(ak=ak, sk=sk, params=params)
        except Exception as exc:
            LOG.warning("Doubao UsageMonitoring failed day=%s: %s", day, exc)
            return ProviderDayUsage(
                provider_id=self.provider_id,
                day=day,
                status="error",
                error=str(exc),
            )

        metrics = _parse_usage_monitoring_response(raw)
        return ProviderDayUsage(
            provider_id=self.provider_id,
            day=day,
            status="ok",
            metrics=metrics,
            raw=raw,
        )


def _parse_usage_monitoring_response(raw: Any) -> dict[str, float]:
    metrics: dict[str, float] = {}
    if not isinstance(raw, dict):
        return metrics
    data = raw.get("data") if isinstance(raw.get("data"), (dict, list)) else raw
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("usage_monitoring", "UsageMonitoring", "items", "list", "Result"):
            node = data.get(key)
            if isinstance(node, list):
                rows = node
                break
        if not rows:
            rows = [data]
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in ("value", "Value", "usage", "Usage", "count", "Count"):
            n = _as_float(row.get(k))
            if n is not None:
                total += n
                break
    if total:
        metrics["provider_usage_units"] = total
    return metrics


async def _volc_usage_monitoring_request(
    *,
    ak: str,
    sk: str,
    params: dict[str, str],
) -> Any:
    """Call open.volcengineapi.com UsageMonitoring.

    Uses `volcenginesdkcore` if installed; otherwise a minimal HMAC-SHA256 signer.
    """
    try:
        from volcenginesdkcore import ApiClient, Configuration  # type: ignore
        from volcenginesdkcore.rest import ApiException  # type: ignore

        configuration = Configuration()
        configuration.ak = ak
        configuration.sk = sk
        configuration.region = "cn-north-1"
        # Generic custom call path varies by SDK version; fall through to HTTP signer.
        _ = (ApiClient, ApiException, configuration)
    except Exception:
        pass

    return await _signed_openapi_get(
        ak=ak,
        sk=sk,
        service="speech_saas_prod",
        region="cn-north-1",
        params=params,
    )


async def _signed_openapi_get(
    *,
    ak: str,
    sk: str,
    service: str,
    region: str,
    params: dict[str, str],
) -> Any:
    """Minimal Volcengine OpenAPI v4 GET signer (enough for UsageMonitoring)."""
    import hashlib
    import hmac
    from datetime import datetime, timezone
    from urllib.parse import quote, urlencode

    host = "open.volcengineapi.com"
    method = "GET"
    path = "/"
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")

    # Canonical query (sorted).
    sorted_items = sorted((str(k), str(v)) for k, v in params.items())
    canonical_query = urlencode(sorted_items, quote_via=quote)

    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = f"host:{host}\nx-date:{x_date}\n"
    signed_headers = "host;x-date"
    canonical_request = "\n".join(
        [
            method,
            path,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            x_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(sk.encode("utf-8"), short_date)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = f"https://{host}/?{canonical_query}"
    headers = {
        "Host": host,
        "X-Date": x_date,
        "Authorization": authorization,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"text": (resp.text or "")[:2000]}
        if resp.status_code >= 400:
            raise RuntimeError(f"UsageMonitoring HTTP {resp.status_code}: {data}")
        return data
