# -*- coding: utf-8 -*-
"""Predict runners: LiveKit v1 (streaming) and v1-mini (batch)."""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

from eot_bench import DATASET_PATH, DEFAULT_SPLIT
from eot_bench.models import ModelSpec, assert_keys_for_models, parse_models
from eot_bench.util import (
    compute_run_metrics,
    ensure_repo_dotenv,
    load_adapter,
    load_language_rows,
    parse_langs,
    resolve_output_dir,
    write_json,
)

LOG = logging.getLogger(__name__)


async def run_predict(
    *,
    models: list[str] | str | None = None,
    langs: list[str] | str | None = None,
    limit: int | None = 20,
    split: str = DEFAULT_SPLIT,
    dataset_path: str = DATASET_PATH,
    output_dir: str | Path | None = None,
    concurrency: int | None = None,
    skip_errors: bool = True,
    overwrite: bool = True,
    compute_metrics: bool = True,
) -> dict[str, Any]:
    """Run one or more EoT models on eot-bench-data; return JSON-serializable payload."""
    ensure_repo_dotenv()
    specs = parse_models(models)
    assert_keys_for_models(specs)
    languages = parse_langs(langs, default=("zh",))
    out_root = resolve_output_dir(output_dir)

    results: list[dict[str, Any]] = []
    for spec in specs:
        for lang in languages:
            started = time.perf_counter()
            try:
                if spec.kind == "streaming":
                    item = await _predict_streaming(
                        spec,
                        lang=lang,
                        limit=limit,
                        split=split,
                        dataset_path=dataset_path,
                        output_root=out_root,
                        concurrency=concurrency,
                        skip_errors=skip_errors,
                        overwrite=overwrite,
                        compute_metrics=compute_metrics,
                    )
                else:
                    item = await asyncio.to_thread(
                        _predict_batch,
                        spec,
                        lang=lang,
                        limit=limit,
                        split=split,
                        dataset_path=dataset_path,
                        output_root=out_root,
                        overwrite=overwrite,
                        compute_metrics=compute_metrics,
                    )
            except Exception as exc:  # noqa: BLE001
                LOG.exception("eot predict failed model=%s lang=%s", spec.id, lang)
                item = {
                    "model": spec.id,
                    "display_name": spec.display_name,
                    "language": lang,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": round(time.perf_counter() - started, 3),
                }
            else:
                item["elapsed_s"] = round(time.perf_counter() - started, 3)
            results.append(item)

    return {
        "dataset": dataset_path,
        "split": split,
        "limit": limit,
        "output_dir": str(out_root),
        "runs": results,
    }


async def run_smoke(
    *,
    models: list[str] | str | None = "v1",
    langs: list[str] | str | None = "zh",
    limit: int = 10,
    concurrency: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience wrapper: small limit, metrics on, skip API errors."""
    # Cloud v1 rate-limits aggressively; keep smoke concurrency low by default.
    conc = 2 if concurrency is None else concurrency
    return await run_predict(
        models=models,
        langs=langs,
        limit=max(1, int(limit)),
        concurrency=conc,
        skip_errors=True,
        compute_metrics=True,
        **kwargs,
    )


async def _predict_streaming(
    spec: ModelSpec,
    *,
    lang: str,
    limit: int | None,
    split: str,
    dataset_path: str,
    output_root: Path,
    concurrency: int | None,
    skip_errors: bool,
    overwrite: bool,
    compute_metrics: bool,
) -> dict[str, Any]:
    from eot_harness.io import DEFAULT_INFERENCE_INTERVAL, DEFAULT_MIN_SILENCE, build_span_set

    adapter = load_adapter(spec)
    rows = load_language_rows(lang=lang, split=split, limit=limit, dataset_path=dataset_path)
    span_rows = build_span_set(rows, min_silence_span=DEFAULT_MIN_SILENCE)
    if not span_rows:
        raise RuntimeError(f"empty span set for language={lang}")

    run_dir = _run_dir(output_root, dataset_path, split, lang, spec)
    if run_dir.exists() and not overwrite:
        pred_path = run_dir / "predictions.parquet"
        if pred_path.is_file():
            payload = _reuse_run(spec, lang, rows, run_dir, compute_metrics=compute_metrics)
            payload["reused"] = True
            return payload
        raise FileExistsError(f"run dir exists but incomplete: {run_dir} (pass overwrite=True)")

    run_dir.mkdir(parents=True, exist_ok=True)
    conc = int(concurrency or getattr(adapter, "concurrency", 4) or 4)
    conc = max(1, conc)
    sem = asyncio.Semaphore(conc)
    inference_interval = DEFAULT_INFERENCE_INTERVAL

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                result = adapter.predict_turn(row, inference_interval=inference_interval)
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict):
                    raise TypeError("streaming adapter must return dict")
                language = str(row.get("language") or lang).strip().lower()
                result.setdefault("language", language)
                for pred in result.get("prediction_rows") or []:
                    if isinstance(pred, dict):
                        pred.setdefault("language", language)
                return result
            except Exception as exc:  # noqa: BLE001
                if not skip_errors:
                    raise
                return {
                    "id": row.get("id"),
                    "language": row.get("language") or lang,
                    "skipped": True,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "events": [],
                    "prediction_rows": [],
                    "audio_sec": 0.0,
                }

    tasks = [asyncio.create_task(one(row)) for row in rows]
    results = await asyncio.gather(*tasks)

    prediction_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    scored_turns = 0
    audio_sec = 0.0
    for result in results:
        if result.get("skipped"):
            skipped.append(
                {
                    "id": result.get("id"),
                    "language": result.get("language") or lang,
                    "reason": result.get("reason"),
                }
            )
            continue
        scored_turns += 1
        audio_sec += float(result.get("audio_sec") or 0.0)
        for pred in result.get("prediction_rows") or []:
            prediction_rows.append(dict(pred))
        for event in result.get("events") or []:
            event_rows.append({"id": result.get("id"), **dict(event)})

    if not prediction_rows:
        reasons = [s.get("reason") for s in skipped[:3]]
        raise RuntimeError(
            f"streaming model {spec.id} produced no predictions for {lang}; "
            f"scored=0 skipped={len(skipped)} sample_errors={reasons}"
        )

    pred_path = run_dir / "predictions.parquet"
    pd.DataFrame(prediction_rows).to_parquet(pred_path, index=False)
    if event_rows:
        pd.DataFrame(event_rows).to_parquet(run_dir / "events.parquet", index=False)
    if skipped:
        pd.DataFrame(skipped).to_parquet(run_dir / "skipped.parquet", index=False)

    manifest = {
        "model": spec.id,
        "display_name": spec.display_name,
        "adapter_id": getattr(adapter, "adapter_id", spec.adapter_ref),
        "adapter": spec.adapter_ref,
        "kind": spec.kind,
        "dataset_path": dataset_path,
        "split": split,
        "language": lang,
        "limit": limit,
        "n_turns_requested": len(rows),
        "n_turns_scored": scored_turns,
        "n_turns_skipped": len(skipped),
        "audio_sec": round(audio_sec, 3),
        "n_prediction_rows": len(prediction_rows),
        "n_spans": len(span_rows),
        "score_point": float(getattr(adapter, "score_point", spec.score_point) or spec.score_point),
        "concurrency": conc,
        "inference_interval": inference_interval,
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(
        run_dir / "summary.json",
        {
            "scored_turns": scored_turns,
            "skipped_turns": len(skipped),
            "audio_sec": round(audio_sec, 3),
            "elapsed_hint": "see parent payload elapsed_s",
        },
    )

    metrics = None
    if compute_metrics:
        metrics = compute_run_metrics(pred_path, score_point=float(manifest["score_point"]))

    close = getattr(adapter, "close", None)
    if callable(close):
        close()

    return {
        "model": spec.id,
        "display_name": spec.display_name,
        "language": lang,
        "n_turns": scored_turns,
        "n_skipped": len(skipped),
        "run_dir": str(run_dir),
        "predictions": str(pred_path),
        "metrics": metrics,
        "reused": False,
    }


def _predict_batch(
    spec: ModelSpec,
    *,
    lang: str,
    limit: int | None,
    split: str,
    dataset_path: str,
    output_root: Path,
    overwrite: bool,
    compute_metrics: bool,
) -> dict[str, Any]:
    from eot_harness.io import (
        DEFAULT_INFERENCE_INTERVAL,
        DEFAULT_MIN_SILENCE,
        DEFAULT_TRANSCRIPT_LAG,
        build_span_set,
        iter_batches,
        iter_transformed_row,
    )

    adapter = load_adapter(spec)
    rows = load_language_rows(lang=lang, split=split, limit=limit, dataset_path=dataset_path)
    span_rows = build_span_set(rows, min_silence_span=DEFAULT_MIN_SILENCE)
    if not span_rows:
        raise RuntimeError(f"empty span set for language={lang}")

    run_dir = _run_dir(output_root, dataset_path, split, lang, spec)
    if run_dir.exists() and not overwrite:
        pred_path = run_dir / "predictions.parquet"
        if pred_path.is_file():
            payload = _reuse_run(spec, lang, rows, run_dir, compute_metrics=compute_metrics)
            payload["reused"] = True
            return payload
        raise FileExistsError(f"run dir exists but incomplete: {run_dir}")

    run_dir.mkdir(parents=True, exist_ok=True)
    max_audio_sec = getattr(adapter, "max_audio_sec", None)
    prediction_rows: list[dict[str, Any]] = []
    batch_items: list[dict[str, Any]] = []

    for row in rows:
        for item in iter_transformed_row(
            row,
            inference_interval=DEFAULT_INFERENCE_INTERVAL,
            transcript_lag=DEFAULT_TRANSCRIPT_LAG,
            min_silence_span=DEFAULT_MIN_SILENCE,
            max_audio_sec=max_audio_sec,
        ):
            batch_items.append(item)

    scores: list[float] = []
    for batch in iter_batches(batch_items, batch_size=32):
        batch_scores = adapter.predict_batch(batch)
        if len(batch_scores) != len(batch):
            raise RuntimeError(
                f"batch adapter returned {len(batch_scores)} scores for {len(batch)} items"
            )
        scores.extend(float(s) for s in batch_scores)

    if len(scores) != len(batch_items):
        raise RuntimeError(f"score count mismatch: {len(scores)} vs {len(batch_items)}")

    for item, score in zip(batch_items, scores):
        prediction_rows.append(
            {
                "id": item["id"],
                "language": item.get("language") or lang,
                "span_index": item["span_index"],
                "timestamp": item["timestamp"],
                "silence_dur": item["silence_dur"],
                "p_eot": float(score),
                "label": item["label"],
            }
        )

    pred_path = run_dir / "predictions.parquet"
    pd.DataFrame(prediction_rows).to_parquet(pred_path, index=False)
    manifest = {
        "model": spec.id,
        "display_name": spec.display_name,
        "adapter_id": getattr(adapter, "adapter_id", spec.adapter_ref),
        "adapter": spec.adapter_ref,
        "kind": spec.kind,
        "dataset_path": dataset_path,
        "split": split,
        "language": lang,
        "limit": limit,
        "n_turns_requested": len(rows),
        "n_turns_scored": len(rows),
        "n_prediction_rows": len(prediction_rows),
        "n_spans": len(span_rows),
        "score_point": float(getattr(adapter, "score_point", spec.score_point) or spec.score_point),
    }
    write_json(run_dir / "manifest.json", manifest)

    metrics = None
    if compute_metrics:
        metrics = compute_run_metrics(pred_path, score_point=float(manifest["score_point"]))

    close = getattr(adapter, "close", None)
    if callable(close):
        close()

    return {
        "model": spec.id,
        "display_name": spec.display_name,
        "language": lang,
        "n_turns": len(rows),
        "n_skipped": 0,
        "run_dir": str(run_dir),
        "predictions": str(pred_path),
        "metrics": metrics,
        "reused": False,
    }


def _run_dir(output_root: Path, dataset_path: str, split: str, lang: str, spec: ModelSpec) -> Path:
    ds_slug = dataset_path.replace("/", "__")
    return output_root / f"{ds_slug}__{split}__min_silence_100ms" / lang / spec.id


def _reuse_run(
    spec: ModelSpec,
    lang: str,
    rows: list[dict[str, Any]],
    run_dir: Path,
    *,
    compute_metrics: bool,
) -> dict[str, Any]:
    pred_path = run_dir / "predictions.parquet"
    metrics = None
    if compute_metrics:
        metrics = compute_run_metrics(pred_path, score_point=spec.score_point)
    return {
        "model": spec.id,
        "display_name": spec.display_name,
        "language": lang,
        "n_turns": len(rows),
        "n_skipped": 0,
        "run_dir": str(run_dir),
        "predictions": str(pred_path),
        "metrics": metrics,
    }
