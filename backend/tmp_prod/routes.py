# -*- coding: utf-8 -*-
"""tmp-prod demo API + results page backing endpoints."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend.outbound.service import verify_partner_api_key
from backend.tmp_prod.excel_io import read_contacts_from_xlsx, write_results_xlsx
from backend.tmp_prod.service import ensure_tmp_prod_env, run_batch
from backend.tmp_prod.store import get_batch_store

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/tmp-prod", tags=["tmp-prod-demo"])


def _auth(x_api_key: str | None) -> None:
    if not verify_partner_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class StartBody(BaseModel):
    identity_mode: str = Field(
        default="BRAND_SELF",
        description="BRAND_SELF | CLIENT_IDENTITY | CHANNEL_BRAND",
    )
    client_name: str = Field(default="比赛日科技", description="委托方 / 自称品牌")
    our_brand_name: str = Field(default="芝麻背调", description="BRAND_SELF 我方品牌")
    channel_brand_name: str = Field(default="", description="CHANNEL_BRAND 渠道品牌名")
    stagger_sec: float = Field(
        default=0.5,
        ge=0,
        le=60,
        description="仅启动间隔（秒），不是等上一通挂机；默认 0.5",
    )
    max_concurrent: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="本批次同时外呼上限；空则 TMP_PROD_MAX_CONCURRENT / OUTBOUND_MAX_CONCURRENT",
    )


def render_tmp_prod_page() -> HTMLResponse:
    """HTML page (also mounted at /tmp-prod without trailing-slash redirect)."""
    path = Path(__file__).resolve().parents[2] / "frontend" / "tmp_prod.html"
    if not path.exists():
        return HTMLResponse("<p>tmp_prod.html not found</p>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")


def render_tmp_prod_single_page() -> HTMLResponse:
    """单人外呼联调页：tmp-prod 剧本 + TTFS 延迟可视化。"""
    import json

    path = Path(__file__).resolve().parents[2] / "frontend" / "tmp_prod_single.html"
    if not path.exists():
        return HTMLResponse("<p>tmp_prod_single.html not found</p>", status_code=404)
    raw = path.read_text(encoding="utf-8")
    try:
        from backend.outbound.dial import load_dial_settings

        cfg = load_dial_settings()
        boot = {
            "called_prefix": cfg.called_prefix,
            "ani": cfg.ani,
            "default_display": cfg.default_display,
            "display_numbers": list(cfg.display_numbers),
            "scenario": "tmp_prod_auth_demo",
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("tmp-prod single boot config failed: %s", exc)
        boot = {
            "called_prefix": "a1",
            "ani": "",
            "default_display": "",
            "display_numbers": [],
            "scenario": "tmp_prod_auth_demo",
            "error": str(exc),
        }

    nums = [str(n).strip() for n in (boot.get("display_numbers") or []) if str(n).strip()]
    default = str(boot.get("default_display") or (nums[0] if nums else "")).strip()
    status = (
        f"外显号由服务端自动分配（默认 {default or '-'}，号池 {len(nums)} 个）。主叫 ANI={boot.get('ani') or '-'}"
        if nums
        else "未配置外显号池，请检查 config.json outbound.display_numbers / SIP_DEFAULT_DISPLAY"
    )
    raw = raw.replace("<!--__STATUS_BOOT__-->", status, 1)
    boot_json = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
    raw = raw.replace("<!--__TMP_PROD_SINGLE_BOOT_JSON__-->", boot_json, 1)
    return HTMLResponse(raw, media_type="text/html; charset=utf-8")


@router.post("/batches/upload")
async def upload_batch(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    ensure_tmp_prod_env()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    try:
        contacts = read_contacts_from_xlsx(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Excel 解析失败: {exc}") from exc
    if not contacts:
        raise HTTPException(status_code=400, detail="未解析到有效联系人（需用人单位/姓名/手机号）")
    store = get_batch_store()
    batch = store.create_batch(contacts, source_name=file.filename or "upload.xlsx")
    # save original
    dest = Path("data/tmp_prod") / batch.batch_id / (file.filename or "upload.xlsx")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return {
        "batch_id": batch.batch_id,
        "source_name": batch.source_name,
        "count": len(batch.contacts),
        "contacts": [
            {
                "contact_id": c.contact_id,
                "employer": c.employer,
                "name": c.name,
                "gender": c.gender,
                "phone": c.phone,
            }
            for c in batch.contacts
        ],
    }


@router.post("/batches/{batch_id}/start")
async def start_batch(
    batch_id: str,
    body: StartBody | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    store = get_batch_store()
    batch = store.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    body = body or StartBody()
    asyncio.create_task(
        run_batch(
            batch_id,
            identity_mode=body.identity_mode,
            client_name=body.client_name,
            our_brand_name=body.our_brand_name,
            channel_brand_name=body.channel_brand_name,
            stagger_sec=body.stagger_sec,
            max_concurrent=body.max_concurrent,
        ),
        name=f"tmp-prod-batch-{batch_id}",
    )
    return {
        "batch_id": batch_id,
        "status": "running",
        "message": "已开始外呼任务（后台并发执行）",
        "stagger_sec": body.stagger_sec,
        "max_concurrent": body.max_concurrent,
    }


@router.get("/batches")
async def list_batches(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    items = get_batch_store().list_batches()
    return {
        "batches": [
            {
                "batch_id": b.batch_id,
                "source_name": b.source_name,
                "status": b.status,
                "created_at": b.created_at,
                "stats": b.stats(),
            }
            for b in items
        ]
    }


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(x_api_key)
    batch = get_batch_store().get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch.to_dict()


@router.get("/batches/{batch_id}/export.xlsx")
async def export_batch_xlsx(
    batch_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    _auth(x_api_key)
    batch = get_batch_store().get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    data = write_results_xlsx([c.to_dict() for c in batch.contacts])
    filename = f"tmp_prod_results_{batch_id}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
