# -*- coding: utf-8 -*-
"""Shared helpers: dataset load, adapter factory, metrics summary."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from eot_bench import DATASET_PATH, DEFAULT_OUTPUT_DIR, DEFAULT_SPLIT, REPO_ROOT
from eot_bench.models import ModelSpec

LOG = logging.getLogger(__name__)

# Leaderboard operating points (same budgets as eot-bench README).
LATENCY_BUDGETS_S = (0.3, 0.6)
CUTOFF_BUDGETS = (0.05, 0.10)


def ensure_repo_dotenv() -> None:
    """Load project-root .env (not eot_harness/.env)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = REPO_ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def resolve_output_dir(raw: str | Path | None = None) -> Path:
    if raw is None or str(raw).strip() == "":
        path = DEFAULT_OUTPUT_DIR
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def parse_langs(raw: str | list[str] | None, *, default: tuple[str, ...] = ("zh",)) -> list[str]:
    if raw is None or raw == "" or raw == []:
        return list(default)
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    return parts or list(default)


def load_adapter(spec: ModelSpec):
    from eot_harness.adapters import load_adapter as _load_batch
    from eot_harness.adapters import load_streaming_adapter

    if spec.kind == "streaming":
        return load_streaming_adapter(spec.adapter_ref)
    return _load_batch(spec.adapter_ref)


def load_language_rows(
    *,
    lang: str,
    split: str = DEFAULT_SPLIT,
    limit: int | None = None,
    dataset_path: str = DATASET_PATH,
) -> list[dict[str, Any]]:
    from eot_harness.io import load_hf_dataset

    token = (os.environ.get("HF_TOKEN") or "").strip() or None
    LOG.info("loading dataset %s name=%s split=%s limit=%s", dataset_path, lang, split, limit)
    ds = load_hf_dataset(dataset_path, lang, split, token=token)
    rows = [dict(row) for row in ds]
    if limit is not None:
        lim = int(limit)
        if lim <= 0:
            raise ValueError("limit must be positive")
        rows = rows[:lim]
    if not rows:
        raise RuntimeError(f"dataset {dataset_path}:{lang}/{split} produced no rows")
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_run_metrics(predictions_path: Path, *, score_point: float = 0.2) -> dict[str, Any]:
    """Run official eot_harness policy sweep; return compact summary for UI/CLI."""
    import pandas as pd
    from eot_harness.metrics import compute_metrics_from_predictions, get_pareto_operating_points

    predictions_df = pd.read_parquet(predictions_path)
    tradeoff_df, summary = compute_metrics_from_predictions(
        predictions_df,
        score_point_s=float(score_point),
    )
    metrics_dir = predictions_path.parent / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tradeoff_df.to_parquet(metrics_dir / "tradeoff.parquet", index=False)
    write_json(metrics_dir / "summary.json", summary)

    cutoff_ops = get_pareto_operating_points(tradeoff_df, "cutoff_rate", CUTOFF_BUDGETS)
    latency_ops = get_pareto_operating_points(tradeoff_df, "mean_latency", LATENCY_BUDGETS_S)

    compact = {
        "score_point_s": float(score_point),
        "n_prediction_rows": int(len(predictions_df)),
        "classification": {
            "auc": summary.get("auc"),
            "ap": summary.get("ap"),
            "n_spans": summary.get("n_spans"),
            "n_eot": summary.get("n_eot_spans"),
            "n_hold": summary.get("n_hold_spans"),
        },
        "latency_at_cutoff_budget": _compact_ops(cutoff_ops, budget_key="cutoff_rate"),
        "cutoff_at_latency_budget": _compact_ops(latency_ops, budget_key="mean_latency"),
        "mean_frontier_latency_0_10": summary.get("mean_frontier_latency_0_10"),
        "metrics_dir": str(metrics_dir),
    }
    write_json(metrics_dir / "compact.json", compact)
    return compact


def _compact_ops(ops_df, *, budget_key: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if ops_df is None or ops_df.empty:
        return rows
    for _, row in ops_df.iterrows():
        budget = row.get("budget_value", row.get("budget"))
        feasible = bool(row.get("feasible", True))
        item: dict[str, Any] = {
            "budget": float(budget) if budget is not None else None,
            "budget_column": budget_key,
            "feasible": feasible,
            "cutoff_rate": _maybe_float(row.get("cutoff_rate")),
            "mean_latency": _maybe_float(row.get("mean_latency")),
            "threshold": _maybe_float(row.get("threshold")),
            "action_delay": _maybe_float(row.get("action_delay")),
            "timeout": _maybe_float(row.get("timeout")),
        }
        rows.append(item)
    return rows


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        import math

        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def print_compact_summary(payload: dict[str, Any]) -> None:
    print(f"model={payload.get('model')}  lang={payload.get('language')}  turns={payload.get('n_turns')}")
    if payload.get("error"):
        print(f"  error: {payload['error']}")
        return
    m = payload.get("metrics") or {}
    clf = m.get("classification") or {}
    print(
        f"  spans={clf.get('n_spans')} eot={clf.get('n_eot')} hold={clf.get('n_hold')} "
        f"auc={_fmt(clf.get('auc'))} ap={_fmt(clf.get('ap'))}"
    )
    for item in m.get("latency_at_cutoff_budget") or []:
        b = item.get("budget")
        if b is None:
            continue
        pct = int(round(float(b) * 100))
        print(
            f"  latency @{pct}% cutoff: "
            f"{_fmt_ms(item.get('mean_latency'))}  "
            f"(thr={_fmt(item.get('threshold'))} delay={_fmt(item.get('action_delay'))}s "
            f"timeout={_fmt(item.get('timeout'))}s feasible={item.get('feasible')})"
        )
    for item in m.get("cutoff_at_latency_budget") or []:
        b = item.get("budget")
        if b is None:
            continue
        print(
            f"  false-cutoff @{int(round(float(b)*1000))}ms latency: "
            f"{_fmt_pct(item.get('cutoff_rate'))}  "
            f"(thr={_fmt(item.get('threshold'))} delay={_fmt(item.get('action_delay'))}s "
            f"feasible={item.get('feasible')})"
        )
    run_dir = payload.get("run_dir")
    if run_dir:
        print(f"  artifacts: {run_dir}")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)*1000:.0f}ms"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)*100:.1f}%"
    except (TypeError, ValueError):
        return str(value)
