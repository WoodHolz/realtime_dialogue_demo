# -*- coding: utf-8 -*-
"""FastAPI routes for /eot-bench and /api/eot-bench/*."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from eot_bench import DATASET_PATH, DEFAULT_SPLIT, WEB_DIR
from eot_bench.models import model_status
from eot_bench.runners.predict import run_predict, run_smoke
from eot_bench.util import ensure_repo_dotenv

router = APIRouter(tags=["eot_bench"])


class SmokeRequest(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["v1"])
    langs: list[str] = Field(default_factory=lambda: ["zh"])
    limit: int = 10
    concurrency: int | None = None


class PredictRequest(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["v1"])
    langs: list[str] = Field(default_factory=lambda: ["zh"])
    limit: int = 50
    concurrency: int | None = None
    skip_errors: bool = True
    compute_metrics: bool = True


@router.get("/eot-bench")
async def eot_bench_page() -> HTMLResponse:
    path = WEB_DIR / "index.html"
    if not path.is_file():
        return HTMLResponse("<p>eot_bench/web/index.html not found</p>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8")


@router.get("/api/eot-bench/meta")
async def eot_bench_meta() -> dict[str, Any]:
    ensure_repo_dotenv()
    return {
        "dataset": DATASET_PATH,
        "default_split": DEFAULT_SPLIT,
        "models": model_status(),
        "help": (
            "Smoke: POST /api/eot-bench/smoke with {models:['v1'], langs:['zh'], limit:10}. "
            "Needs LIVEKIT_API_KEY + LIVEKIT_API_SECRET for v1. "
            "Dataset: livekit/eot-bench-data (HF)."
        ),
    }


@router.post("/api/eot-bench/smoke")
async def eot_bench_smoke(body: SmokeRequest) -> dict[str, Any]:
    ensure_repo_dotenv()
    try:
        return await run_smoke(
            models=body.models,
            langs=body.langs,
            limit=max(1, min(body.limit, 100)),
            concurrency=body.concurrency,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/eot-bench/predict")
async def eot_bench_predict(body: PredictRequest) -> dict[str, Any]:
    ensure_repo_dotenv()
    try:
        return await run_predict(
            models=body.models,
            langs=body.langs,
            limit=max(1, min(body.limit, 500)),
            concurrency=body.concurrency,
            skip_errors=body.skip_errors,
            compute_metrics=body.compute_metrics,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
