# -*- coding: utf-8 -*-
"""Cancellable stub TTS: 24 kHz s16le frames (quiet sine) so barge-in can be heard."""
from __future__ import annotations

import asyncio
import logging
import math
import struct
from collections.abc import Awaitable, Callable

LOG = logging.getLogger(__name__)

TTS_RATE = 24000
FRAME_MS = 20
SAMPLES_PER_FRAME = TTS_RATE * FRAME_MS // 1000
FRAMES_PER_SENTENCE = 12  # 240ms

AudioCb = Callable[[bytes], Awaitable[None] | None]
StartCb = Callable[[], Awaitable[None] | None]
EndCb = Callable[[], Awaitable[None] | None]


def _sine_frame(phase: float, freq: float = 330.0, amplitude: int = 1200) -> tuple[bytes, float]:
    samples: list[int] = []
    step = 2.0 * math.pi * freq / TTS_RATE
    for _ in range(SAMPLES_PER_FRAME):
        samples.append(int(amplitude * math.sin(phase)))
        phase += step
    return struct.pack("<" + "h" * len(samples), *samples), phase


class StubTts:
    def __init__(
        self,
        *,
        on_audio: AudioCb | None = None,
        on_start: StartCb | None = None,
        on_end: EndCb | None = None,
        frames_per_sentence: int = FRAMES_PER_SENTENCE,
    ) -> None:
        self.on_audio = on_audio
        self.on_start = on_start
        self.on_end = on_end
        self.frames_per_sentence = frames_per_sentence
        self.fed: list[str] = []
        self._started = False
        self._cancelled = False
        self._phase = 0.0
        self._feed_tasks: list[asyncio.Task] = []
        self._gen_id = ""

    def bind(
        self,
        *,
        on_audio: AudioCb | None = None,
        on_start: StartCb | None = None,
        on_end: EndCb | None = None,
    ) -> None:
        if on_audio:
            self.on_audio = on_audio
        if on_start:
            self.on_start = on_start
        if on_end:
            self.on_end = on_end

    async def start(self, *, voice: str, sample_rate: int = TTS_RATE, gen_id: str = "") -> None:
        self._cancelled = False
        self._started = True
        self._phase = 0.0
        self._gen_id = gen_id
        self.fed.clear()
        if self.on_start:
            await self._maybe_await(self.on_start())

    async def feed(self, text: str) -> None:
        if self._cancelled or not self._started:
            return
        self.fed.append(text)
        task = asyncio.create_task(self._emit_sentence())
        self._feed_tasks.append(task)

    async def flush(self) -> None:
        pending = [t for t in self._feed_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._feed_tasks.clear()
        if self._cancelled or not self._started:
            return
        self._started = False
        if self.on_end:
            await self._maybe_await(self.on_end())

    async def cancel(self) -> None:
        self._cancelled = True
        self._started = False
        for task in self._feed_tasks:
            if not task.done():
                task.cancel()
        if self._feed_tasks:
            await asyncio.gather(*self._feed_tasks, return_exceptions=True)
        self._feed_tasks.clear()

    async def _emit_sentence(self) -> None:
        try:
            for _ in range(self.frames_per_sentence):
                if self._cancelled:
                    return
                frame, self._phase = _sine_frame(self._phase)
                if self.on_audio:
                    await self._maybe_await(self.on_audio(frame))
                await asyncio.sleep(FRAME_MS / 1000.0)
        except asyncio.CancelledError:
            return

    @staticmethod
    async def _maybe_await(result: object) -> None:
        if asyncio.iscoroutine(result):
            await result
