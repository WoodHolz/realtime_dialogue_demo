# -*- coding: utf-8 -*-
"""Streaming adapter contracts. Cancel is keyed by turn_id / gen_id."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.pipeline.dialogue import HistoryTurn


@runtime_checkable
class StreamingAsr(Protocol):
    async def start(self, *, turn_id: str = "") -> None: ...

    async def send_pcm(self, pcm16_16k: bytes, *, is_last: bool = False) -> None: ...

    async def wait_final(self) -> str: ...

    async def cancel(self) -> None: ...


@runtime_checkable
class StreamingLlm(Protocol):
    async def start_speculative(
        self,
        *,
        system: str,
        history: list[HistoryTurn],
        user_text: str,
        gen_id: str,
    ) -> None: ...

    async def revise(self, *, user_text: str, gen_id: str) -> None: ...

    async def commit(self, *, final_text: str, gen_id: str) -> None: ...

    async def cancel(self, gen_id: str) -> None: ...


@runtime_checkable
class StreamingTts(Protocol):
    async def start(self, *, voice: str, sample_rate: int = 24000, gen_id: str = "") -> None: ...

    async def feed(self, text: str) -> None: ...

    async def flush(self) -> None: ...

    async def cancel(self) -> None: ...
