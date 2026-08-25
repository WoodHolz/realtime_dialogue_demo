# -*- coding: utf-8 -*-
"""StreamingLlm adapter over DashScope OpenAI-compatible Responses API.

Vendor HTTP details stay in this file. Orchestrator only sees start/revise/commit/cancel.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.pipeline.dialogue import HistoryTurn

LOG = logging.getLogger(__name__)

DeltaCb = Callable[[str, str], Awaitable[None]]
SentenceCb = Callable[[str, str], Awaitable[None]]
DoneCb = Callable[[str], Awaitable[None]]


def build_responses_input(
    system: str,
    history: list[HistoryTurn],
    user_text: str,
) -> list[dict[str, str]]:
    """Conversation turns only. System prompt goes in `instructions`."""
    _ = system
    items: list[dict[str, str]] = []
    for turn in history:
        role = "assistant" if turn.role == "assistant" else "user"
        text = (turn.text or "").strip()
        if not text:
            continue
        items.append({"role": role, "content": text})
    items.append({"role": "user", "content": (user_text or "").strip()})
    return items


def output_text_delta(event: Any) -> str:
    """Keep message tokens; drop reasoning/thinking deltas so TTS never speaks thoughts."""
    etype = ""
    delta = ""
    if isinstance(event, dict):
        etype = str(event.get("type") or "")
        delta = event.get("delta") or ""
        if not delta and isinstance(event.get("text"), str):
            delta = event["text"]
    else:
        etype = str(getattr(event, "type", "") or "")
        delta = getattr(event, "delta", "") or ""
        if not delta:
            delta = getattr(event, "text", "") or ""
    etype_l = etype.lower()
    if "reasoning" in etype_l or "thinking" in etype_l:
        return ""
    if etype_l in {
        "response.output_text.delta",
        "response.text.delta",
    }:
        return str(delta)
    return ""


class DashScopeResponsesLlm:
    """StreamingLlm backed by POST /responses (stream=True). Not a ChatCompletions client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        enable_thinking: bool = False,
        timeout_sec: float = 60.0,
        on_delta: DeltaCb | None = None,
        on_sentence: SentenceCb | None = None,
        on_done: DoneCb | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self.enable_thinking = bool(enable_thinking)
        self.timeout_sec = timeout_sec
        self.on_delta = on_delta
        self.on_sentence = on_sentence
        self.on_done = on_done
        self._system = ""
        self._history: list[HistoryTurn] = []
        self._active_gen = ""
        self._task: asyncio.Task | None = None
        self._stream = None
        self._cancelled: set[str] = set()

    def bind(
        self,
        *,
        on_delta: DeltaCb | None = None,
        on_sentence: SentenceCb | None = None,
        on_done: DoneCb | None = None,
    ) -> None:
        if on_delta:
            self.on_delta = on_delta
        if on_sentence:
            self.on_sentence = on_sentence
        if on_done:
            self.on_done = on_done

    async def start_speculative(
        self,
        *,
        system: str,
        history: list[HistoryTurn],
        user_text: str,
        gen_id: str,
    ) -> None:
        if not self.api_key or not self.base_url or not self.model:
            raise RuntimeError(
                "DashScopeResponsesLlm needs api_key, base_url, and model "
                "(DASHSCOPE_API_KEY; default host is https://dashscope.aliyuncs.com/compatible-mode/v1)"
            )
        await self.cancel(self._active_gen)
        self._system = system or ""
        self._history = list(history)
        self._active_gen = gen_id
        self._cancelled.discard(gen_id)
        self._task = asyncio.create_task(self._stream_gen(gen_id, user_text))

    async def revise(self, *, user_text: str, gen_id: str) -> None:
        await self.start_speculative(
            system=self._system,
            history=self._history,
            user_text=user_text,
            gen_id=gen_id,
        )

    async def commit(self, *, final_text: str, gen_id: str) -> None:
        return

    async def cancel(self, gen_id: str) -> None:
        if not gen_id:
            return
        self._cancelled.add(gen_id)
        if self._active_gen == gen_id:
            self._active_gen = ""
        stream = self._stream
        self._stream = None
        if stream is not None:
            close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    LOG.debug("responses stream close failed", exc_info=True)
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _stream_gen(self, gen_id: str, user_text: str) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for pipeline LLM: pip install openai") from exc
        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_sec,
        )
        payload_input = build_responses_input(self._system, self._history, user_text)
        LOG.info(
            "DashScopeResponsesLlm gen_id=%s hist=%s input_items=%s",
            gen_id,
            len(self._history),
            len(payload_input),
        )
        # stream=True always; thinking off by default (enable_thinking=false).
        # instructions = system prompt; input = multi-turn messages (not a lone user string).
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "input": payload_input,
            "instructions": self._system or None,
            "stream": True,
            "extra_body": {"enable_thinking": bool(self.enable_thinking)},
        }
        if not create_kwargs["instructions"]:
            create_kwargs.pop("instructions")
        try:
            stream = await client.responses.create(**create_kwargs)
            self._stream = stream
            async for event in stream:
                if gen_id in self._cancelled:
                    return
                # Official path: only response.output_text.delta → spoken text.
                chunk = output_text_delta(event)
                if chunk and self.on_delta:
                    try:
                        await self.on_delta(gen_id, chunk)
                    except Exception:
                        # Downstream TTS must not abort the Responses stream.
                        LOG.warning(
                            "DashScopeResponsesLlm on_delta failed gen_id=%s",
                            gen_id,
                            exc_info=True,
                        )
            if self.on_done and gen_id not in self._cancelled:
                try:
                    await self.on_done(gen_id)
                except Exception:
                    LOG.warning("DashScopeResponsesLlm on_done failed gen_id=%s", gen_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception:
            LOG.exception("DashScopeResponsesLlm stream failed gen_id=%s", gen_id)
            raise
        finally:
            self._stream = None
            try:
                await client.close()
            except Exception:
                pass
