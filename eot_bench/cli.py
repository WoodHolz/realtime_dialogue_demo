# -*- coding: utf-8 -*-
"""CLI for eot_bench smoke / predict.

Usage (from repo root):
  python -m eot_bench.cli smoke --model v1 --lang zh --limit 10
  python -m eot_bench.cli predict --models v1,v1-mini --langs zh,en --limit 50
  python -m eot_bench.cli meta
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from eot_bench import DATASET_PATH, DEFAULT_OUTPUT_DIR, DEFAULT_SPLIT
from eot_bench.models import model_status
from eot_bench.runners.predict import run_predict, run_smoke
from eot_bench.util import ensure_repo_dotenv, print_compact_summary


def _parse_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


async def _cmd_smoke(args: argparse.Namespace) -> int:
    try:
        payload = await run_smoke(
            models=_parse_csv(args.models) or ["v1"],
            langs=_parse_csv(args.langs) or ["zh"],
            limit=args.limit,
            split=args.split,
            dataset_path=args.dataset,
            output_dir=args.output_dir or None,
            concurrency=args.concurrency,
            overwrite=not args.no_overwrite,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return _emit(payload, as_json=args.as_json)


async def _cmd_predict(args: argparse.Namespace) -> int:
    try:
        payload = await run_predict(
            models=_parse_csv(args.models) or ["v1"],
            langs=_parse_csv(args.langs) or ["zh"],
            limit=args.limit,
            split=args.split,
            dataset_path=args.dataset,
            output_dir=args.output_dir or None,
            concurrency=args.concurrency,
            skip_errors=not args.fail_fast,
            overwrite=not args.no_overwrite,
            compute_metrics=not args.no_metrics,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return _emit(payload, as_json=args.as_json)


def _cmd_meta(_args: argparse.Namespace) -> int:
    payload = {
        "dataset": DATASET_PATH,
        "default_split": DEFAULT_SPLIT,
        "default_output_dir": str(DEFAULT_OUTPUT_DIR),
        "models": model_status(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _emit(payload: dict, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dataset={payload.get('dataset')} split={payload.get('split')} limit={payload.get('limit')}")
        print(f"output={payload.get('output_dir')}")
        for run in payload.get("runs") or []:
            print_compact_summary(run)
            print()
    runs = payload.get("runs") or []
    ok = any(not r.get("error") and (r.get("n_turns") or 0) > 0 for r in runs if isinstance(r, dict))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="eot_bench: LiveKit Turn Detector on eot-bench-data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--models", "-m", default="v1", help="Comma-separated: v1,v1-mini")
        p.add_argument("--langs", "-l", default="zh", help="Comma-separated language configs (zh,en,...)")
        p.add_argument("--limit", type=int, default=10, help="Max turns per language (smoke default 10)")
        p.add_argument("--split", default=DEFAULT_SPLIT)
        p.add_argument("--dataset", default=DATASET_PATH)
        p.add_argument("--output-dir", default="", help=f"Default: {DEFAULT_OUTPUT_DIR}")
        p.add_argument("--concurrency", type=int, default=0, help="Streaming concurrency (0=adapter default)")
        p.add_argument("--no-overwrite", action="store_true", help="Reuse existing run dirs when complete")
        p.add_argument("--json", action="store_true", dest="as_json")

    sp = sub.add_parser("smoke", help="Quick smoke: small limit + metrics")
    add_common(sp)
    sp.set_defaults(func=_cmd_smoke, limit=10)

    pp = sub.add_parser("predict", help="Fuller predict run (still supports --limit)")
    add_common(pp)
    pp.add_argument("--fail-fast", action="store_true", help="Abort on first streaming row error")
    pp.add_argument("--no-metrics", action="store_true")
    pp.set_defaults(func=_cmd_predict, limit=50)

    mp = sub.add_parser("meta", help="Show models / dataset defaults")
    mp.set_defaults(func=_cmd_meta)

    return ap


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ensure_repo_dotenv()
    args = build_parser().parse_args(argv)
    if args.cmd == "meta":
        raise SystemExit(args.func(args))
    # Normalize concurrency 0 → None
    if getattr(args, "concurrency", None) == 0:
        args.concurrency = None
    raise SystemExit(asyncio.run(args.func(args)))


if __name__ == "__main__":
    main()
