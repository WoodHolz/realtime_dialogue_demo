# -*- coding: utf-8 -*-
"""StreamingLlm adapter for Xiaomi MiMo OpenAI-compatible Responses API.

POST https://api.xiaomimimo.com/v1/responses
Auth: api-key header (also accepts Authorization: Bearer).

Env: MIMO_API_KEY, optional MIMO_BASE_URL / MIMO_MODEL.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.pipeline.adapters.llm_dashscope import output_text_delta
from backend.pipeline.dialogue import HistoryTurn

LOG = logging.getLogger(__name__)

DeltaCb = Callable[[str, str], Awaitable[None]]
SentenceCb = Callable[[str, str], Awaitable[None]]
DoneCb = Callable[[str], Awaitable[None]]

DEFAULT_MIMO_BASE = "https://api.xiaomimimo.com/v1"
DEFAULT_MIMO_MODEL = "mimo-v2.5-pro"


def build_mimo_responses_input(
    history: list[HistoryTurn],
    user_text: str,
) -> list[dict[str, Any]]:
    """MiMo Responses input items (role + typed content parts)."""
    items: list[dict[str, Any]] = []
    for turn in history:
        role = "assistant" if turn.role == "assistant" else "user"
        text = (turn.text or "").strip()
        if not text:
            continue
        part_type = "output_text" if role == "assistant" else "input_text"
        items.append({"role": role, "content": [{"type": part_type, "text": text}]})
    items.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": (user_text or "").strip()}],
        }
    )
    return items


class MimoResponsesLlm:
    """StreamingLlm via Xiaomi MiMo POST /responses (stream=True)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_MIMO_BASE,
        model: str = DEFAULT_MIMO_MODEL,
        enable_thinking: bool = False,
        max_output_tokens: int = 1024,
        timeout_sec: float = 90.0,
        on_delta: DeltaCb | None = None,
        on_sentence: SentenceCb | None = None,
        on_done: DoneCb | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_MIMO_BASE).strip().rstrip("/")
        self.model = (model or DEFAULT_MIMO_MODEL).strip()
        self.enable_thinking = bool(enable_thinking)
        self.max_output_tokens = max(1, int(max_output_tokens or 1024))
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
        self.last_error: BaseException | None = None

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
                "MimoResponsesLlm needs MIMO_API_KEY, base_url, and model "
                f"(default host {DEFAULT_MIMO_BASE})"
            )
        await self.cancel(self._active_gen)
        self._system = system or ""
        self._history = list(history)
        self._active_gen = gen_id
        self._cancelled.discard(gen_id)
        self.last_error = None
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
                    LOG.debug("mimo responses stream close failed", exc_info=True)
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
            raise RuntimeError("openai package is required for MiMo LLM: pip install openai") from exc

        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_sec,
            # Docs accept either Authorization: Bearer or api-key header.
            default_headers={"api-key": self.api_key},
        )
        payload_input = build_mimo_responses_input(self._history, user_text)
        extra_body: dict[str, Any] = {}
        # Disable thinking/reasoning by default for low-latency bench / voice path.
        if not self.enable_thinking:
            extra_body["reasoning"] = {"effort": "none"}
        LOG.info(
            "MimoResponsesLlm gen_id=%s model=%s hist=%s thinking=%s",
            gen_id,
            self.model,
            len(self._history),
            self.enable_thinking,
        )
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "input": payload_input,
            "instructions": self._system or None,
            "stream": True,
            "max_output_tokens": self.max_output_tokens,
        }
        if not create_kwargs["instructions"]:
            create_kwargs.pop("instructions")
        if extra_body:
            create_kwargs["extra_body"] = extra_body
        try:
            stream = await client.responses.create(**create_kwargs)
            self._stream = stream
            async for event in stream:
                if gen_id in self._cancelled:
                    return
                etype = ""
                if isinstance(event, dict):
                    etype = str(event.get("type") or "")
                else:
                    etype = str(getattr(event, "type", "") or "")
                if etype == "response.failed":
                    err_msg = "MiMo responses failed"
                    try:
                        resp = getattr(event, "response", None) or (
                            event.get("response") if isinstance(event, dict) else None
                        )
                        err = getattr(resp, "error", None) if resp is not None else None
                        if err is None and isinstance(resp, dict):
                            err = resp.get("error")
                        if err is not None:
                            err_msg = str(getattr(err, "message", None) or err)
                    except Exception:
                        pass
                    self.last_error = RuntimeError(err_msg)
                    LOG.error("MimoResponsesLlm response.failed gen_id=%s: %s", gen_id, err_msg)
                    break
                chunk = output_text_delta(event)
                if chunk and self.on_delta:
                    try:
                        await self.on_delta(gen_id, chunk)
                    except Exception:
                        LOG.warning("MimoResponsesLlm on_delta failed gen_id=%s", gen_id, exc_info=True)
            if self.on_done and gen_id not in self._cancelled:
                try:
                    await self.on_done(gen_id)
                except Exception:
                    LOG.warning("MimoResponsesLlm on_done failed gen_id=%s", gen_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.last_error = exc
            LOG.exception("MimoResponsesLlm stream failed gen_id=%s", gen_id)
            if self.on_done and gen_id not in self._cancelled:
                try:
                    await self.on_done(gen_id)
                except Exception:
                    LOG.warning("MimoResponsesLlm on_done failed gen_id=%s", gen_id, exc_info=True)
            return
        finally:
            self._stream = None
            try:
                await client.close()
            except Exception:
                pass
