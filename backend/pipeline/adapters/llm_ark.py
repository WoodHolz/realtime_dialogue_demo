# -*- coding: utf-8 -*-
"""StreamingLlm adapter over Volcengine Ark OpenAI-compatible Chat Completions.

Env: ARK_API_KEY, optional ARK_BASE_URL (default https://ark.cn-beijing.volces.com/api/v3).
Used by bench for speed/recovery A/B vs DashScope; not wired into live pipeline yet.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from backend.pipeline.dialogue import HistoryTurn

LOG = logging.getLogger(__name__)

DeltaCb = Callable[[str, str], Awaitable[None]]
SentenceCb = Callable[[str, str], Awaitable[None]]
DoneCb = Callable[[str], Awaitable[None]]

DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
# Low-latency tier after Ark Console activates "fast mode" for the model.
DEFAULT_ARK_SERVICE_TIER = "fast"


def resolve_ark_service_tier(raw: str | None = None) -> str | None:
    """Return service_tier for create(), or None to omit the field.

    Env ARK_SERVICE_TIER: fast (default) | off | default | auto.
    """
    import os

    text = (raw if raw is not None else os.getenv("ARK_SERVICE_TIER", DEFAULT_ARK_SERVICE_TIER) or "").strip().lower()
    if not text or text in {"off", "none", "false", "0", "-"}:
        return None
    return text


def _is_fast_tier_not_open(exc: BaseException) -> bool:
    text = str(exc)
    return "ModelNotOpen" in text or "not activated the fast mode" in text.lower()


def build_chat_messages(
    system: str,
    history: list[HistoryTurn],
    user_text: str,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    sys = (system or "").strip()
    if sys:
        items.append({"role": "system", "content": sys})
    for turn in history:
        role = "assistant" if turn.role == "assistant" else "user"
        text = (turn.text or "").strip()
        if not text:
            continue
        items.append({"role": role, "content": text})
    items.append({"role": "user", "content": (user_text or "").strip()})
    return items


class ArkChatCompletionsLlm:
    """StreamingLlm via POST /chat/completions (stream=True)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_ARK_BASE,
        model: str,
        service_tier: str | None = DEFAULT_ARK_SERVICE_TIER,
        extra_body: dict | None = None,
        timeout_sec: float = 60.0,
        on_delta: DeltaCb | None = None,
        on_sentence: SentenceCb | None = None,
        on_done: DoneCb | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_ARK_BASE).strip().rstrip("/")
        self.model = (model or "").strip()
        self.service_tier = resolve_ark_service_tier(service_tier)
        self.extra_body = dict(extra_body or {})
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
                "ArkChatCompletionsLlm needs ARK_API_KEY, base_url, and model "
                f"(default host {DEFAULT_ARK_BASE})"
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
                    LOG.debug("ark chat stream close failed", exc_info=True)
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
            raise RuntimeError("openai package is required for Ark LLM: pip install openai") from exc
        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_sec,
        )
        messages = build_chat_messages(self._system, self._history, user_text)
        LOG.info(
            "ArkChatCompletionsLlm gen_id=%s model=%s service_tier=%s hist=%s messages=%s",
            gen_id,
            self.model,
            self.service_tier or "-",
            len(self._history),
            len(messages),
        )
        try:
            # stream only as SDK top-level param (do not duplicate in extra_body).
            create_kwargs: dict = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            body_extra = dict(self.extra_body)
            if self.service_tier:
                body_extra["service_tier"] = self.service_tier
            if body_extra:
                create_kwargs["extra_body"] = body_extra
            try:
                stream = await client.chat.completions.create(**create_kwargs)
            except TypeError:
                create_kwargs.pop("extra_body", None)
                for k, v in body_extra.items():
                    create_kwargs[k] = v
                create_kwargs["stream"] = True
                stream = await client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                # Account has not activated Ark "fast mode" for this model → retry without tier.
                if self.service_tier and _is_fast_tier_not_open(exc):
                    LOG.warning(
                        "Ark service_tier=%s not activated for model=%s; retrying without it. "
                        "Activate fast mode in Ark Console, or set ARK_SERVICE_TIER=off.",
                        self.service_tier,
                        self.model,
                    )
                    self.service_tier = None
                    create_kwargs["stream"] = True
                    create_kwargs.pop("extra_body", None)
                    create_kwargs.pop("service_tier", None)
                    body_retry = dict(self.extra_body)
                    if body_retry:
                        create_kwargs["extra_body"] = body_retry
                    stream = await client.chat.completions.create(**create_kwargs)
                else:
                    raise
            self._stream = stream
            async for event in stream:
                if gen_id in self._cancelled:
                    return
                chunk = ""
                try:
                    choices = getattr(event, "choices", None) or []
                    if choices:
                        delta = getattr(choices[0], "delta", None)
                        chunk = (getattr(delta, "content", None) or "") if delta else ""
                except Exception:
                    chunk = ""
                if chunk and self.on_delta:
                    try:
                        await self.on_delta(gen_id, str(chunk))
                    except Exception:
                        LOG.warning("ArkChatCompletionsLlm on_delta failed gen_id=%s", gen_id, exc_info=True)
            if self.on_done and gen_id not in self._cancelled:
                try:
                    await self.on_done(gen_id)
                except Exception:
                    LOG.warning("ArkChatCompletionsLlm on_done failed gen_id=%s", gen_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.last_error = exc
            LOG.exception("ArkChatCompletionsLlm stream failed gen_id=%s", gen_id)
            if self.on_done and gen_id not in self._cancelled:
                try:
                    await self.on_done(gen_id)
                except Exception:
                    LOG.warning("ArkChatCompletionsLlm on_done failed gen_id=%s", gen_id, exc_info=True)
            return
        finally:
            self._stream = None
            try:
                await client.close()
            except Exception:
                pass
