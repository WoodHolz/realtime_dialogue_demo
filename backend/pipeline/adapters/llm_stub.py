# -*- coding: utf-8 -*-
"""Cancellable stub LLM: echoes the user snapshot as two sentences."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from backend.pipeline.dialogue import HistoryTurn

LOG = logging.getLogger(__name__)

DeltaCb = Callable[[str, str], Awaitable[None]]
SentenceCb = Callable[[str, str], Awaitable[None]]
DoneCb = Callable[[str], Awaitable[None]]


class StubLlm:
    """Deterministic streaming echo for interrupt-path tests. No vendor SDK."""

    def __init__(
        self,
        *,
        on_delta: DeltaCb | None = None,
        on_sentence: SentenceCb | None = None,
        on_done: DoneCb | None = None,
        chunk_delay_sec: float = 0.01,
    ) -> None:
        self.on_delta = on_delta
        self.on_sentence = on_sentence
        self.on_done = on_done
        self.chunk_delay_sec = chunk_delay_sec
        self.calls: list[dict[str, object]] = []
        self._system = ""
        self._history: list[HistoryTurn] = []
        self._active_gen = ""
        self._task: asyncio.Task | None = None
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
        await self.cancel(self._active_gen)
        self._system = system
        self._history = list(history)
        self._active_gen = gen_id
        self._cancelled.discard(gen_id)
        self.calls.append(
            {
                "gen_id": gen_id,
                "user_text": user_text,
                "history": [(t.role, t.text) for t in history],
                "system": system,
            }
        )
        self._task = asyncio.create_task(self._stream(gen_id, user_text))

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
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _stream(self, gen_id: str, user_text: str) -> None:
        body = (user_text or "").strip() or "（空）"
        reply = f"收到。{body}。"
        try:
            for i in range(0, len(reply), 2):
                if gen_id in self._cancelled:
                    return
                chunk = reply[i : i + 2]
                if self.on_delta:
                    await self.on_delta(gen_id, chunk)
                if self.chunk_delay_sec > 0:
                    await asyncio.sleep(self.chunk_delay_sec)
            if self.on_done and gen_id not in self._cancelled:
                await self.on_done(gen_id)
        except asyncio.CancelledError:
            return
        except Exception:
            LOG.debug("stub LLM stream failed gen_id=%s", gen_id, exc_info=True)
