# -*- coding: utf-8 -*-
"""FastAPI routes for /bench and /api/bench/*."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backend.pipeline.config import load_pipeline_settings
from bench import WEB_DIR
from bench.prompt import DEFAULT_QUERY
from bench.providers import DEFAULT_ARK_MODEL, provider_status, suggested_bench_models
from bench.runners.recovery import list_recovery_cases, run_recovery
from bench.runners.speed import run_speed

router = APIRouter(tags=["bench"])


class SpeedRequest(BaseModel):
    models: list[str] = Field(default_factory=list)
    query: str = DEFAULT_QUERY
    runs: int = 1
    llm_only: bool = True
    pause: float = 0.5
    no_tts_prompt: bool = False
    # Same as recovery: concat SUPERVISOR_AUDIT for apples-to-apples latency.
    supervisor_audit: bool = False


class RecoveryRequest(BaseModel):
    models: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    pause: float = 0.3
    no_tts_prompt: bool = False
    # supervisor = standalone Supervisor prompt (default, accurate for旁路)
    # fast = pipeline manuscript only
    # fast_audit = legacy concat SUPERVISOR_AUDIT onto fast prompt
    prompt_mode: str = "supervisor"


@router.get("/bench")
async def bench_page() -> HTMLResponse:
    path = WEB_DIR / "index.html"
    if not path.is_file():
        return HTMLResponse("<p>bench/web/index.html not found</p>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")


@router.get("/api/bench/meta")
async def bench_meta() -> dict[str, Any]:
    settings = load_pipeline_settings()
    suggested = suggested_bench_models(settings)
    # Default input: public dashscope + kimi + ark; MaaS models are one-click presets.
    default_compare = ",".join(
        item["id"]
        for item in suggested
        if item.get("provider") in {"dashscope", "kimi", "mimo", "ark"}
    )
    return {
        "default_query": DEFAULT_QUERY,
        "default_model": settings.llm_model,
        "default_models_compare": default_compare,
        "default_ark_model": DEFAULT_ARK_MODEL,
        "suggested_models": suggested,
        "providers": provider_status(settings),
        "settings": settings.describe(),
        "cases": list_recovery_cases(),
        "model_id_help": (
            "Prefixes: dashscope:qwen3.8-flash | dashscope:qwen3.8-2.4t-a95b | "
            "kimi:kimi-k2.6 | mimo:mimo-v2.5-pro | ark:doubao-…. "
            "Kimi thinking off by default (thinking.type=disabled). "
            "MiMo uses Responses API + MIMO_API_KEY. "
            "Add +thinking to opt in. Bare kimi-* / mimo-* auto-route."
        ),
    }


@router.get("/api/bench/cases")
async def bench_cases() -> dict[str, Any]:
    return {"cases": list_recovery_cases()}


@router.post("/api/bench/speed")
async def bench_speed(body: SpeedRequest) -> dict[str, Any]:
    try:
        return await run_speed(
            models=body.models,
            query=body.query or DEFAULT_QUERY,
            runs=max(1, min(body.runs, 20)),
            llm_only=body.llm_only,
            pause=max(0.0, body.pause),
            no_tts_prompt=body.no_tts_prompt,
            supervisor_audit=body.supervisor_audit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/bench/recovery")
async def bench_recovery(body: RecoveryRequest) -> dict[str, Any]:
    try:
        return await run_recovery(
            models=body.models,
            case_ids=body.case_ids,
            pause=max(0.0, body.pause),
            no_tts_prompt=body.no_tts_prompt,
            prompt_mode=body.prompt_mode or "supervisor",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
