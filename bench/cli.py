#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI for bench speed + recovery runners.

Usage (from repo root):
  python -m bench.cli speed --runs 3 --llm-only
  python -m bench.cli recovery --models qwen3.5-flash
  python -m bench.cli recovery --cases noncompete_denied,refuse_salary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from backend.pipeline.config import load_pipeline_settings
from bench.prompt import DEFAULT_QUERY
from bench.runners.recovery import print_recovery_summary, run_recovery
from bench.runners.speed import print_speed_summary, run_speed


def _parse_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


async def _cmd_speed(args: argparse.Namespace) -> int:
    try:
        payload = await run_speed(
            models=_parse_csv(args.models),
            query=args.query,
            runs=args.runs,
            llm_only=args.llm_only,
            system_override=args.system,
            no_tts_prompt=args.no_tts_prompt,
            supervisor_audit=args.supervisor_audit,
            pause=args.pause,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        cfg = payload.get("settings") or {}
        print(
            f"LLM adapter={cfg.get('llm_adapter')}  TTS adapter={cfg.get('tts_adapter')}  "
            f"thinking={cfg.get('enable_thinking')}"
        )
        print_speed_summary(payload)
    models = payload.get("models") or []
    any_ok = any(int(m.get("ok_runs") or 0) > 0 for m in models if isinstance(m, dict))
    return 0 if any_ok else 1


async def _cmd_recovery(args: argparse.Namespace) -> int:
    try:
        payload = await run_recovery(
            models=_parse_csv(args.models),
            case_ids=_parse_csv(args.cases),
            system_override=args.system,
            no_tts_prompt=args.no_tts_prompt,
            supervisor_audit=not args.no_supervisor_audit,
            pause=args.pause,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_recovery_summary(payload)
    models = payload.get("models") or []
    ran = any(int(m.get("total") or 0) > 0 for m in models if isinstance(m, dict))
    return 0 if ran else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Pipeline bench: speed + recovery")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("speed", help="TTFT / TTFS / chars/s")
    sp.add_argument("--query", "-q", default=DEFAULT_QUERY)
    sp.add_argument(
        "--models",
        "-m",
        default="",
        help="Comma-separated model ids, e.g. dashscope:qwen3.5-flash,maas:qwen3.8-flash,ark:doubao-seed-2-1-turbo-260628",
    )
    sp.add_argument("--system", "-s", default="")
    sp.add_argument("--no-tts-prompt", action="store_true")
    sp.add_argument(
        "--supervisor-audit",
        action="store_true",
        help="Append SUPERVISOR_AUDIT (same prompt as recovery completeness tests)",
    )
    sp.add_argument("--runs", type=int, default=1)
    sp.add_argument("--llm-only", action="store_true")
    sp.add_argument("--json", action="store_true", dest="as_json")
    sp.add_argument("--pause", type=float, default=0.5)
    sp.set_defaults(func=_cmd_speed)

    rp = sub.add_parser("recovery", help="纠偏 case 规则评测")
    rp.add_argument(
        "--models",
        "-m",
        default="",
        help="Comma-separated model ids, e.g. dashscope:qwen3.5-flash,maas:qwen3.8-flash,ark:doubao-seed-2-1-turbo-260628",
    )
    rp.add_argument("--cases", "-c", default="", help="Comma-separated case ids (default: all)")
    rp.add_argument("--system", "-s", default="")
    rp.add_argument("--no-tts-prompt", action="store_true")
    rp.add_argument(
        "--no-supervisor-audit",
        action="store_true",
        help="Do not append SUPERVISOR_AUDIT completeness/branch prompt",
    )
    rp.add_argument("--json", action="store_true", dest="as_json")
    rp.add_argument("--pause", type=float, default=0.3)
    rp.set_defaults(func=_cmd_recovery)

    return ap


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING)
    # Ensure settings/env load happens before runners (dotenv via load_pipeline_settings).
    load_pipeline_settings()
    args = build_parser().parse_args(argv)
    raise SystemExit(asyncio.run(args.func(args)))


if __name__ == "__main__":
    main()
