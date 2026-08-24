# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

from backend.pipeline.orchestrator import PipelineOrchestrator
from backend.pipeline.textutil import SentenceBuffer, normalize_user_text, user_prompts_aligned
from backend.pipeline.adapters.llm_stub import StubLlm
from backend.pipeline.adapters.tts_stub import StubTts


class FakeAsr:
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False
        self.pcm: list[tuple[bytes, bool]] = []
        self.final = ""
        self._done = asyncio.Event()

    async def start(self, *, turn_id: str = "") -> None:
        self.started = True
        self.cancelled = False
        self._done = asyncio.Event()

    async def send_pcm(self, pcm16_16k: bytes, *, is_last: bool = False) -> None:
        self.pcm.append((pcm16_16k, is_last))
        if is_last:
            self._done.set()

    async def wait_final(self) -> str:
        await asyncio.wait_for(self._done.wait(), timeout=2.0)
        return self.final

    async def cancel(self) -> None:
        self.cancelled = True
        self._done.set()


def test_normalize_and_alignment():
    assert normalize_user_text(" 帮我查一下。 ") == "帮我查一下"
    assert user_prompts_aligned("帮我查一下", "帮我查一下。")
    assert not user_prompts_aligned("帮我查一下", "帮我查一下上海")
    assert not user_prompts_aligned("我下周", "我下周可能周三吧")
    assert not user_prompts_aligned("", "hello")


def test_sentence_buffer_splits_chinese():
    buf = SentenceBuffer()
    assert buf.feed("收到。") == ["收到。"]
    assert buf.feed("帮我") == []
    assert buf.feed("查一下。") == ["帮我查一下。"]
    assert buf.flush() == ""


def _orch() -> tuple[PipelineOrchestrator, StubLlm, StubTts, list[dict], list[bytes], FakeAsr]:
    events: list[dict] = []
    pcm_out: list[bytes] = []
    llm = StubLlm(chunk_delay_sec=0.0)
    tts = StubTts(frames_per_sentence=8)
    asr = FakeAsr()
    orch = PipelineOrchestrator(
        llm=llm,
        tts=tts,
        asr=asr,
        emit_json=events.append,
        emit_pcm=lambda b: pcm_out.append(b),
        system_role="你是访谈员",
        voice="stub",
        speculate_debounce_sec=0.02,
        tts_barge_suppress_ms=0,
    )
    return orch, llm, tts, events, pcm_out, asr


async def _wait_until(pred, timeout: float = 1.5) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_prefix_growth_restarts_llm_and_blocks_early_tts():
    asyncio.run(_prefix_growth_restarts_llm_and_blocks_early_tts())


async def _prefix_growth_restarts_llm_and_blocks_early_tts():
    orch, llm, tts, _events, _pcm, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("帮我查一下", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    assert llm.calls[0]["user_text"] == "帮我查一下"
    gen1 = llm.calls[0]["gen_id"]
    await asyncio.sleep(0.03)
    assert tts.fed == []
    await orch.on_asr_partial("帮我查一下上海", False)
    await _wait_until(lambda: len(llm.calls) >= 2)
    assert llm.calls[-1]["user_text"] == "帮我查一下上海"
    assert llm.calls[-1]["gen_id"] != gen1
    assert tts.fed == []
    await orch.close()


def test_eou_mismatch_discards_short_prompt_generation():
    asyncio.run(_eou_mismatch_discards_short_prompt_generation())


async def _eou_mismatch_discards_short_prompt_generation():
    orch, llm, tts, events, _pcm, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("我下周", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="我下周可能周三吧")
    await _wait_until(lambda: any(e.get("type") == "llm_committed" for e in events))
    assert llm.calls[-1]["user_text"] == "我下周可能周三吧"
    assert any(e.get("phase") == "discarded" for e in events if e.get("type") == "pipeline_turn")
    committed = [e for e in events if e.get("type") == "llm_committed"]
    assert committed[-1]["user_text"] == "我下周可能周三吧"
    await _wait_until(lambda: bool(tts.fed))
    assert any("我下周可能周三吧" in s for s in tts.fed)
    await orch.close()


def test_eou_aligned_reuses_generation():
    asyncio.run(_eou_aligned_reuses_generation())


async def _eou_aligned_reuses_generation():
    orch, llm, tts, events, _pcm, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("帮我查一下上海", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    gen_id = llm.calls[0]["gen_id"]
    await orch.on_eou(final_text="帮我查一下上海。")
    await _wait_until(lambda: any(e.get("type") == "llm_committed" for e in events))
    assert llm.calls[-1]["gen_id"] == gen_id
    assert not any(e.get("phase") == "discarded" for e in events if e.get("type") == "pipeline_turn")
    await orch.close()


def test_barge_in_keeps_spoken_prefix_in_history():
    asyncio.run(_barge_in_keeps_spoken_prefix_in_history())


async def _barge_in_keeps_spoken_prefix_in_history():
    orch, llm, tts, events, pcm_out, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: bool(pcm_out) or bool(tts.fed))
    await asyncio.sleep(0.05)
    barged = await orch.on_speech_start(source="vad")
    assert barged
    assert any(e.get("type") == "barge_in" for e in events)
    assert any(e.get("type") == "tts_cancel" for e in events)
    roles = [(t.role, t.text) for t in orch.state.history]
    assert roles[0] == ("user", "你好")
    assert roles[1][0] == "assistant"
    assert roles[1][1]
    await orch.close()


def test_restart_resends_committed_history_only():
    asyncio.run(_restart_resends_committed_history_only())


async def _restart_resends_committed_history_only():
    orch, llm, tts, events, pcm_out, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("第一轮", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="第一轮")
    await _wait_until(lambda: bool(pcm_out) or bool(tts.fed))
    await _wait_until(lambda: orch._llm_done, timeout=1.0)
    await asyncio.sleep(0.08)
    if not any(t.role == "assistant" for t in orch.state.history):
        orch._finalize_history(orch.state.spoken_text or "收到。第一轮。")
        orch.state.phase = "idle"
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("第二轮", False)
    await _wait_until(lambda: any(c["user_text"] == "第二轮" for c in llm.calls))
    second = next(c for c in reversed(llm.calls) if c["user_text"] == "第二轮")
    hist = second["history"]
    assert ("user", "第一轮") in hist
    assert "第二轮" not in [t[1] for t in hist]
    await orch.close()


def test_identical_partial_does_not_restart():
    asyncio.run(_identical_partial_does_not_restart())


async def _identical_partial_does_not_restart():
    orch, llm, tts, events, _pcm, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("同一句", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    n = len(llm.calls)
    await orch.on_asr_partial("同一句", False)
    await asyncio.sleep(0.06)
    assert len(llm.calls) == n
    await orch.close()
