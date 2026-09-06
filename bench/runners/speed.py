# -*- coding: utf-8 -*-
"""Speed runner: TTFT / TTFS / avg chars/sec on pipeline LLM→TTS path."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import statistics
import time
import uuid
from dataclasses import dataclass, field

from backend.pipeline.config import PipelineSettings, build_streaming_tts, load_pipeline_settings
from backend.pipeline.textutil import SentenceBuffer
from bench.llm_factory import build_llm_for_model
from bench.prompt import DEFAULT_QUERY, resolve_system_prompt
from bench.providers import assert_keys_for_models, parse_model_spec

LOG = logging.getLogger(__name__)


def _format_llm_error(exc: BaseException, *, model: str = "") -> str:
    text = str(exc)
    if "Endpoint.AccessDenied" in text or "Workspace endpoint access denied" in text:
        return (
            f"{text} | Hint: maas/workspace host needs a key with access; "
            f"or use dashscope:* on public host. model={model}"
        )
    if "API key format is incorrect" in text or "AuthenticationError" in text:
        return (
            f"{text} | Hint: ARK_API_KEY must be from 火山方舟 → API Key 管理 "
            f"(not IAM AK/SK, not DOUBAO_API_KEY). Recreate/copy the key. "
            f"If the account requires an inference endpoint, use model ark:ep-xxxx. model={model}"
        )
    return text


@dataclass
class RunMetrics:
    ttft_ms: float | None = None
    ttfs_ms: float | None = None
    avg_chars_per_sec: float | None = None
    total_chars: int = 0
    total_pcm_bytes: int = 0
    stream_ms: float | None = None
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclass
class ModelSummary:
    model: str
    runs: list[RunMetrics] = field(default_factory=list)

    def mean(self, attr: str) -> float | None:
        vals = [getattr(r, attr) for r in self.runs if getattr(r, attr) is not None and not r.error]
        if not vals:
            return None
        return statistics.mean(vals)

    def stdev(self, attr: str) -> float | None:
        vals = [getattr(r, attr) for r in self.runs if getattr(r, attr) is not None and not r.error]
        if len(vals) < 2:
            return None
        return statistics.stdev(vals)

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "ttft_ms_mean": self.mean("ttft_ms"),
            "ttft_ms_stdev": self.stdev("ttft_ms"),
            "ttfs_ms_mean": self.mean("ttfs_ms"),
            "ttfs_ms_stdev": self.stdev("ttfs_ms"),
            "avg_chars_per_sec_mean": self.mean("avg_chars_per_sec"),
            "avg_chars_per_sec_stdev": self.stdev("avg_chars_per_sec"),
            "total_chars_mean": self.mean("total_chars"),
            "ok_runs": sum(1 for r in self.runs if not r.error),
            "runs": [r.as_dict() for r in self.runs],
        }


async def benchmark_once(
    settings: PipelineSettings,
    *,
    model: str,
    query: str,
    system: str,
    skip_tts: bool,
) -> RunMetrics:
    metrics = RunMetrics()
    llm = build_llm_for_model(settings, model)
    tts = None if skip_tts else build_streaming_tts(settings)

    sentence_buf = SentenceBuffer()
    done_event = asyncio.Event()
    t_query = 0.0
    t_first_token: float | None = None
    t_first_audio: float | None = None
    gen_id = f"bench-{uuid.uuid4().hex[:8]}"

    async def on_delta(_gen_id: str, chunk: str) -> None:
        nonlocal t_first_token
        if t_first_token is None:
            t_first_token = time.perf_counter()
        metrics.total_chars += len(chunk)
        if tts is None:
            return
        for sent in sentence_buf.feed(chunk):
            await tts.feed(sent)

    async def on_done(_gen_id: str) -> None:
        if tts is not None:
            leftover = sentence_buf.flush()
            if leftover:
                await tts.feed(leftover)
            await tts.flush()
        metrics.stream_ms = (time.perf_counter() - t_query) * 1000.0
        done_event.set()

    async def on_audio(pcm: bytes) -> None:
        nonlocal t_first_audio
        if t_first_audio is None and pcm:
            t_first_audio = time.perf_counter()
        metrics.total_pcm_bytes += len(pcm or b"")

    llm.bind(on_delta=on_delta, on_done=on_done)
    if tts is not None:
        tts.bind(on_audio=on_audio)

    try:
        if tts is not None:
            await tts.start(
                voice=settings.tts_voice,
                sample_rate=settings.tts_sample_rate,
                gen_id=gen_id,
            )
        t_query = time.perf_counter()
        await llm.start_speculative(
            system=system,
            history=[],
            user_text=query,
            gen_id=gen_id,
        )
        await asyncio.wait_for(done_event.wait(), timeout=120.0)
        err = getattr(llm, "last_error", None)
        if err is not None and not metrics.error:
            metrics.error = _format_llm_error(err, model=model)
    except asyncio.TimeoutError:
        metrics.error = "timeout"
        await llm.cancel(gen_id)
        if tts is not None:
            await tts.cancel()
    except Exception as exc:
        metrics.error = str(exc)
        LOG.exception("benchmark failed model=%s", model)
        try:
            await llm.cancel(gen_id)
        except Exception:
            pass
        if tts is not None:
            try:
                await tts.cancel()
            except Exception:
                pass
    finally:
        if t_first_token is not None:
            metrics.ttft_ms = (t_first_token - t_query) * 1000.0
            elapsed = time.perf_counter() - t_first_token
            if elapsed > 0 and metrics.total_chars > 0:
                metrics.avg_chars_per_sec = metrics.total_chars / elapsed
        if t_first_audio is not None:
            metrics.ttfs_ms = (t_first_audio - t_query) * 1000.0

    return metrics


async def run_speed(
    *,
    models: list[str] | None = None,
    query: str = DEFAULT_QUERY,
    runs: int = 1,
    llm_only: bool = False,
    system_override: str = "",
    no_tts_prompt: bool = False,
    pause: float = 0.5,
    settings: PipelineSettings | None = None,
) -> dict[str, object]:
    cfg = settings or load_pipeline_settings()
    raw_models = [m.strip() for m in (models or []) if m and m.strip()] or [cfg.llm_model]
    specs = [parse_model_spec(m) for m in raw_models]
    system, prompt_src = resolve_system_prompt(
        system_override=system_override,
        no_tts_prompt=no_tts_prompt,
    )

    assert_keys_for_models(specs, cfg, need_tts=not llm_only)

    summaries: list[ModelSummary] = []
    for spec in specs:
        summary = ModelSummary(model=spec.label)
        for run_idx in range(max(1, runs)):
            metrics = await benchmark_once(
                cfg,
                model=spec.label,
                query=query,
                system=system,
                skip_tts=llm_only,
            )
            summary.runs.append(metrics)
            if run_idx + 1 < runs:
                await asyncio.sleep(max(0.0, pause))
        summaries.append(summary)

    return {
        "query": query,
        "system_source": prompt_src,
        "system_chars": len(system),
        "llm_only": llm_only,
        "settings": cfg.describe(),
        "models": [s.as_dict() for s in summaries],
    }


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}"


def _fmt_speed(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def print_speed_summary(payload: dict[str, object]) -> None:
    models = payload.get("models") or []
    skip_tts = bool(payload.get("llm_only"))
    print()
    print(f"Query: {payload.get('query')!r}")
    print(f"Prompt: {payload.get('system_source')} ({payload.get('system_chars')} chars)")
    print(f"TTS: {'off (LLM only)' if skip_tts else 'on (pipeline path)'}")
    print()

    header = f"{'Model':<28} {'TTFT(ms)':>10} {'Chars/s':>10} {'Chars':>7} {'Runs':>5}"
    if not skip_tts:
        header = f"{'Model':<28} {'TTFT(ms)':>10} {'TTFS(ms)':>10} {'Chars/s':>10} {'Chars':>7} {'Runs':>5}"
    print(header)
    print("-" * len(header))

    for item in models:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "")
        ttft = item.get("ttft_ms_mean")
        ttfs = item.get("ttfs_ms_mean")
        speed = item.get("avg_chars_per_sec_mean")
        chars = int(item.get("total_chars_mean") or 0)
        n_ok = int(item.get("ok_runs") or 0)
        ttft_sd = item.get("ttft_ms_stdev")
        ttfs_sd = item.get("ttfs_ms_stdev")
        speed_sd = item.get("avg_chars_per_sec_stdev")

        ttft_s = _fmt_ms(ttft if isinstance(ttft, (int, float)) else None)
        if isinstance(ttft_sd, (int, float)):
            ttft_s += f"±{ttft_sd:.0f}"
        speed_s = _fmt_speed(speed if isinstance(speed, (int, float)) else None)
        if isinstance(speed_sd, (int, float)):
            speed_s += f"±{speed_sd:.1f}"

        if skip_tts:
            print(f"{model:<28} {ttft_s:>10} {speed_s:>10} {chars:>7} {n_ok:>5}")
        else:
            ttfs_s = _fmt_ms(ttfs if isinstance(ttfs, (int, float)) else None)
            if isinstance(ttfs_sd, (int, float)):
                ttfs_s += f"±{ttfs_sd:.0f}"
            print(f"{model:<28} {ttft_s:>10} {ttfs_s:>10} {speed_s:>10} {chars:>7} {n_ok:>5}")

        for i, run in enumerate(item.get("runs") or [], 1):
            if not isinstance(run, dict):
                continue
            if run.get("error"):
                print(f"  run {i}: ERROR {run['error']}")
            else:
                parts = [
                    f"ttft={_fmt_ms(run.get('ttft_ms') if isinstance(run.get('ttft_ms'), (int, float)) else None)}ms",
                    f"speed={_fmt_speed(run.get('avg_chars_per_sec') if isinstance(run.get('avg_chars_per_sec'), (int, float)) else None)}",
                ]
                if not skip_tts:
                    parts.append(
                        f"ttfs={_fmt_ms(run.get('ttfs_ms') if isinstance(run.get('ttfs_ms'), (int, float)) else None)}ms"
                    )
                print(f"  run {i}: {', '.join(parts)}")
