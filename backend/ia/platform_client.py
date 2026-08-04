# -*- coding: utf-8 -*-
"""HTTP client: algorithm → AI platform (IA-03A / 03B / 05)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from backend.ia.models import ArtifactUploadMeta, CallEventRequest, CallOutcomeRequest

LOG = logging.getLogger(__name__)


def platform_base_url() -> str:
    return (os.environ.get("IA_PLATFORM_BASE_URL") or "").rstrip("/")


def platform_token() -> str:
    return (os.environ.get("IA_PLATFORM_TOKEN") or "").strip()


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    tok = platform_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def enabled() -> bool:
    """When base URL unset, log-only dry mode (still builds payloads)."""
    return bool(platform_base_url())


class PlatformClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url if base_url is not None else platform_base_url()).rstrip("/")

    async def post_call_event(self, body: CallEventRequest) -> dict[str, Any]:
        return await self._post_json("/report/voice/callEvents", body.model_dump(mode="json", exclude_none=True))

    async def post_call_outcome(self, body: CallOutcomeRequest) -> dict[str, Any]:
        return await self._post_json(
            "/report/voice/callOutcomes",
            body.model_dump(mode="json", exclude_none=True),
        )

    async def upload_recording(
        self,
        *,
        meta: ArtifactUploadMeta,
        file_path: Path,
    ) -> dict[str, Any]:
        path = "/report/voice/recording"
        if not self.base_url:
            LOG.info(
                "IA-05 dry-run meta=%s file=%s",
                meta.model_dump(mode="json", exclude_none=True),
                file_path,
            )
            return {"dry_run": True, "ok": True}

        url = f"{self.base_url}{path}"
        data = {
            "meta": json.dumps(
                meta.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
            )
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with file_path.open("rb") as fh:
                    files = {"file": (file_path.name, fh, "application/octet-stream")}
                    resp = await client.post(url, data=data, files=files, headers=_headers())
                LOG.info(
                    "IA-05 upload status=%s url=%s meta=%s resp=%s",
                    resp.status_code,
                    url,
                    data.get("meta"),
                    (resp.text or "")[:800],
                )
                resp.raise_for_status()
                if resp.content:
                    try:
                        return resp.json()
                    except Exception:
                        return {"ok": True, "text": resp.text}
                return {"ok": True}
        except Exception:
            LOG.exception("IA-05 upload failed path=%s", file_path)
            raise

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            LOG.info("IA platform dry-run POST %s body=%s", path, body)
            return {"dry_run": True, "ok": True}
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=body, headers=_headers())
            LOG.info(
                "IA platform POST %s status=%s body=%s resp=%s",
                path,
                resp.status_code,
                body,
                (resp.text or "")[:800],
            )
            resp.raise_for_status()
            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return {"ok": True, "text": resp.text}
            return {"ok": True}
        except Exception:
            LOG.exception("IA platform POST failed path=%s", path)
            raise


def get_platform_client() -> PlatformClient:
    return PlatformClient()
