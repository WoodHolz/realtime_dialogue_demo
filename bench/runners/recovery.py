# -*- coding: utf-8 -*-
"""Recovery (intelligence) runner: scripted surprise turns + rule scoring."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.pipeline.config import PipelineSettings, load_pipeline_settings
from backend.pipeline.dialogue import HistoryTurn, system_with_progress
from bench import CASES_DIR
from bench.llm_factory import build_llm_for_model
from bench.prompt import resolve_system_prompt
from bench.providers import assert_keys_for_models, parse_model_spec

LOG = logging.getLogger(__name__)

_MD_MARKERS = ("```", "**", "__", "- ", "* ", "# ")
_QUESTION_MARKS = ("？", "?")


@dataclass
class CaseResult:
    case_id: str
    category: str
    title: str
    model: str
    pass_: bool
    reply: str = ""
    failures: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "title": self.title,
            "model": self.model,
            "pass": self.pass_,
            "reply": self.reply,
            "failures": list(self.failures),
            "error": self.error,
        }


def list_recovery_cases(cases_dir: Path | None = None) -> list[dict[str, object]]:
    root = cases_dir or CASES_DIR
    out: list[dict[str, object]] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": str(data.get("id") or path.stem),
                "category": str(data.get("category") or ""),
                "title": str(data.get("title") or data.get("id") or path.stem),
                "path": str(path.name),
            }
        )
    return out


def load_recovery_cases(
    *,
    case_ids: list[str] | None = None,
    cases_dir: Path | None = None,
) -> list[dict[str, Any]]:
    root = cases_dir or CASES_DIR
    wanted = {c.strip() for c in (case_ids or []) if c and c.strip()} or None
    cases: list[dict[str, Any]] = []
    if not root.is_dir():
        return cases
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("skip bad case %s: %s", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        cid = str(data.get("id") or path.stem)
        if wanted is not None and cid not in wanted:
            continue
        data["id"] = cid
        data["_path"] = str(path)
        cases.append(data)
    return cases


def _count_questions(text: str) -> int:
    return sum(text.count(m) for m in _QUESTION_MARKS)


def score_reply(reply: str, expect: dict[str, Any]) -> tuple[bool, list[str]]:
    """Rule-based scoring. Returns (pass, failure reasons)."""
    failures: list[str] = []
    text = (reply or "").strip()
    if not text:
        return False, ["empty_reply"]

    for needle in expect.get("must_not_any") or []:
        if needle and needle in text:
            failures.append(f"must_not_any:{needle}")

    for needle in expect.get("must_not_ask_fields") or []:
        if needle and needle in text:
            failures.append(f"must_not_ask_fields:{needle}")

    for needle in expect.get("must_not_skip_to") or []:
        if needle and needle in text:
            failures.append(f"must_not_skip_to:{needle}")

    must_any = [n for n in (expect.get("must_any") or []) if n]
    if must_any and not any(n in text for n in must_any):
        failures.append(f"must_any_missing:{'|'.join(must_any)}")

    if expect.get("no_markdown"):
        for marker in _MD_MARKERS:
            if marker in text:
                # Allow TTS tags like [serious] — not markdown lists at line start only for "- "
                if marker == "- " and not re.search(r"(?m)^-\s", text):
                    continue
                if marker == "* " and not re.search(r"(?m)^\*\s", text):
                    continue
                failures.append(f"no_markdown:{marker.strip()}")
                break

    q_count = _count_questions(text)
    if "max_questions" in expect:
        try:
            max_q = int(expect["max_questions"])
        except (TypeError, ValueError):
            max_q = 1
        if q_count > max_q:
            failures.append(f"max_questions:{q_count}>{max_q}")
    elif expect.get("single_question") and q_count > 1:
        failures.append(f"single_question:{q_count}>1")

    if expect.get("prefer_stop") and q_count > 0:
        failures.append("prefer_stop:still_asking")

    if expect.get("must_advance"):
        # Soft: reply should not re-ask company confirmation verbatim patterns already checked via must_not
        pass

    if expect.get("prefer_clarify") and q_count < 1:
        # Soft fail: vague answers should usually get a clarifying question
        failures.append("prefer_clarify:no_question")

    return (len(failures) == 0), failures


async def _generate_reply(
    settings: PipelineSettings,
    *,
    model: str,
    system: str,
    history: list[HistoryTurn],
    user_text: str,
    timeout_sec: float = 90.0,
) -> str:
    llm = build_llm_for_model(settings, model)
    chunks: list[str] = []
    done = asyncio.Event()
    gen_id = f"rec-{uuid.uuid4().hex[:8]}"

    async def on_delta(_gid: str, chunk: str) -> None:
        if chunk:
            chunks.append(chunk)

    async def on_done(_gid: str) -> None:
        done.set()

    llm.bind(on_delta=on_delta, on_done=on_done)
    try:
        await llm.start_speculative(
            system=system_with_progress(system, history),
            history=history,
            user_text=user_text,
            gen_id=gen_id,
        )
        await asyncio.wait_for(done.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        await llm.cancel(gen_id)
        raise
    except Exception:
        try:
            await llm.cancel(gen_id)
        except Exception:
            pass
        raise
    err = getattr(llm, "last_error", None)
    if err is not None:
        text = str(err)
        if "Endpoint.AccessDenied" in text or "Workspace endpoint access denied" in text:
            raise RuntimeError(
                f"{text} | Hint: set BENCH_MAAS_API_KEY to a key that can access the "
                f"workspace host (public DASHSCOPE_API_KEY is often denied)."
            ) from err
        raise RuntimeError(text) from err
    return "".join(chunks).strip()


def _history_from_case(case: dict[str, Any]) -> list[HistoryTurn]:
    turns: list[HistoryTurn] = []
    for item in case.get("history") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        turns.append(HistoryTurn(role=role, text=text))
    return turns


async def run_recovery(
    *,
    models: list[str] | None = None,
    case_ids: list[str] | None = None,
    system_override: str = "",
    no_tts_prompt: bool = False,
    pause: float = 0.3,
    settings: PipelineSettings | None = None,
) -> dict[str, object]:
    cfg = settings or load_pipeline_settings()
    raw_models = [m.strip() for m in (models or []) if m and m.strip()] or [cfg.llm_model]
    specs = [parse_model_spec(m) for m in raw_models]
    system, prompt_src = resolve_system_prompt(
        system_override=system_override,
        no_tts_prompt=no_tts_prompt,
    )
    cases = load_recovery_cases(case_ids=case_ids)

    assert_keys_for_models(specs, cfg, need_tts=False)
    if not cases:
        raise RuntimeError(f"no recovery cases found under {CASES_DIR}")

    results: list[CaseResult] = []
    for spec in specs:
        model = spec.label
        for case in cases:
            cid = str(case.get("id") or "")
            category = str(case.get("category") or "")
            title = str(case.get("title") or cid)
            user_text = str(case.get("user") or "").strip()
            expect = case.get("expect") if isinstance(case.get("expect"), dict) else {}
            history = _history_from_case(case)
            try:
                reply = await _generate_reply(
                    cfg,
                    model=model,
                    system=system,
                    history=history,
                    user_text=user_text,
                )
                ok, failures = score_reply(reply, expect)
                results.append(
                    CaseResult(
                        case_id=cid,
                        category=category,
                        title=title,
                        model=model,
                        pass_=ok,
                        reply=reply,
                        failures=failures,
                    )
                )
            except Exception as exc:
                LOG.exception("recovery case failed id=%s model=%s", cid, model)
                results.append(
                    CaseResult(
                        case_id=cid,
                        category=category,
                        title=title,
                        model=model,
                        pass_=False,
                        reply="",
                        failures=["error"],
                        error=str(exc),
                    )
                )
            if pause > 0:
                await asyncio.sleep(pause)

    by_model: dict[str, dict[str, object]] = {}
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        slot = by_model.setdefault(r.model, {"model": r.model, "pass": 0, "total": 0, "results": []})
        slot["total"] = int(slot["total"]) + 1
        if r.pass_:
            slot["pass"] = int(slot["pass"]) + 1
        assert isinstance(slot["results"], list)
        slot["results"].append(r.as_dict())

        cat = by_category.setdefault(r.category or "unknown", {"pass": 0, "total": 0})
        cat["total"] += 1
        if r.pass_:
            cat["pass"] += 1

    for slot in by_model.values():
        total = int(slot["total"]) or 1
        slot["pass_rate"] = float(slot["pass"]) / float(total)

    for cat, slot in by_category.items():
        total = int(slot["total"]) or 1
        slot["pass_rate"] = float(slot["pass"]) / float(total)

    return {
        "system_source": prompt_src,
        "system_chars": len(system),
        "settings": cfg.describe(),
        "case_count": len(cases),
        "models": list(by_model.values()),
        "by_category": by_category,
        "results": [r.as_dict() for r in results],
    }


def print_recovery_summary(payload: dict[str, object]) -> None:
    print()
    print(f"Prompt: {payload.get('system_source')} ({payload.get('system_chars')} chars)")
    print(f"Cases: {payload.get('case_count')}")
    print()
    for model_block in payload.get("models") or []:
        if not isinstance(model_block, dict):
            continue
        rate = model_block.get("pass_rate")
        rate_s = f"{float(rate) * 100:.0f}%" if isinstance(rate, (int, float)) else "-"
        print(
            f"Model {model_block.get('model')}: "
            f"{model_block.get('pass')}/{model_block.get('total')} pass ({rate_s})"
        )
        for item in model_block.get("results") or []:
            if not isinstance(item, dict):
                continue
            mark = "PASS" if item.get("pass") else "FAIL"
            fails = item.get("failures") or []
            err = item.get("error") or ""
            extra = f" [{', '.join(fails)}]" if fails else ""
            if err:
                extra += f" error={err}"
            reply = str(item.get("reply") or "")
            preview = reply[:80] + ("…" if len(reply) > 80 else "")
            print(f"  {mark} {item.get('case_id')}: {preview}{extra}")
    print()
    print("By category:")
    for cat, slot in (payload.get("by_category") or {}).items():
        if not isinstance(slot, dict):
            continue
        rate = slot.get("pass_rate")
        rate_s = f"{float(rate) * 100:.0f}%" if isinstance(rate, (int, float)) else "-"
        print(f"  {cat}: {slot.get('pass')}/{slot.get('total')} ({rate_s})")
