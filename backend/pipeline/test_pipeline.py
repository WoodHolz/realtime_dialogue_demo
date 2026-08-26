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
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("第二轮", False)
    await _wait_until(lambda: any(c["user_text"] == "第二轮" for c in llm.calls))
    second = next(c for c in reversed(llm.calls) if c["user_text"] == "第二轮")
    hist = second["history"]
    assert ("user", "第一轮") in hist
    assert any(r == "assistant" and t for r, t in hist)
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


class _SilentTts:
    """Starts and accepts feed but never fires on_end — TTS hang / WS timeout."""

    def __init__(self) -> None:
        self.fed: list[str] = []
        self.on_audio = None
        self.on_start = None
        self.on_end = None

    def bind(self, *, on_audio=None, on_start=None, on_end=None) -> None:
        if on_audio:
            self.on_audio = on_audio
        if on_start:
            self.on_start = on_start
        if on_end:
            self.on_end = on_end

    async def start(self, *, voice: str, sample_rate: int = 24000, gen_id: str = "") -> None:
        if self.on_start:
            result = self.on_start()
            if asyncio.iscoroutine(result):
                await result

    async def feed(self, text: str) -> None:
        self.fed.append(text)

    async def flush(self) -> None:
        return

    async def cancel(self) -> None:
        return


def test_next_turn_commits_draft_if_tts_never_ends():
    asyncio.run(_next_turn_commits_draft_if_tts_never_ends())


async def _next_turn_commits_draft_if_tts_never_ends():
    events: list[dict] = []
    llm = StubLlm(chunk_delay_sec=0.0)
    tts = _SilentTts()
    asr = FakeAsr()
    orch = PipelineOrchestrator(
        llm=llm,
        tts=tts,
        asr=asr,
        emit_json=events.append,
        system_role="你是访谈员",
        voice="stub",
        speculate_debounce_sec=0.02,
        tts_barge_suppress_ms=0,
    )
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: orch._llm_done)
    await asyncio.sleep(0.05)
    assert not any(t.role == "assistant" for t in orch.state.history)
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("方便", False)
    await _wait_until(lambda: any(c["user_text"] == "方便" for c in llm.calls))
    second = next(c for c in reversed(llm.calls) if c["user_text"] == "方便")
    hist = second["history"]
    assert ("user", "你好") in hist
    assert any(r == "assistant" and "你好" in t for r, t in hist)
    await orch.close()


def test_eou_during_tts_without_speech_start_keeps_history():
    asyncio.run(_eou_during_tts_without_speech_start_keeps_history())


async def _eou_during_tts_without_speech_start_keeps_history():
    orch, llm, tts, events, _pcm, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: orch.state.phase in {"committed", "tts_playing"})
    await _wait_until(lambda: orch._llm_done)
    await asyncio.sleep(0.05)
    # No on_speech_start — same bug as barge-suppress / missed VAD.
    await orch.on_eou(final_text="方便的")
    assert any(e.get("type") == "barge_in" for e in events)
    await _wait_until(lambda: any(c["user_text"] == "方便的" for c in llm.calls))
    second = next(c for c in reversed(llm.calls) if c["user_text"] == "方便的")
    hist = second["history"]
    assert ("user", "你好") in hist
    assert any(r == "assistant" and t for r, t in hist)
    await orch.close()


def test_barge_in_emits_before_slow_tts_cancel():
    asyncio.run(_barge_in_emits_before_slow_tts_cancel())


async def _barge_in_emits_before_slow_tts_cancel():
    class SlowCancelTts:
        def __init__(self) -> None:
            self.fed: list[str] = []
            self.cancel_started = asyncio.Event()
            self.allow_finish = asyncio.Event()
            self.on_audio = None
            self.on_start = None
            self.on_end = None

        def bind(self, *, on_audio=None, on_start=None, on_end=None) -> None:
            if on_audio:
                self.on_audio = on_audio
            if on_start:
                self.on_start = on_start
            if on_end:
                self.on_end = on_end

        async def start(self, *, voice: str, sample_rate: int = 24000, gen_id: str = "") -> None:
            if self.on_start:
                result = self.on_start()
                if asyncio.iscoroutine(result):
                    await result

        async def feed(self, text: str) -> None:
            self.fed.append(text)
            if self.on_audio:
                result = self.on_audio(b"first")
                if asyncio.iscoroutine(result):
                    await result

        async def flush(self) -> None:
            return

        async def cancel(self) -> None:
            self.cancel_started.set()
            await self.allow_finish.wait()

    events: list[dict] = []
    pcm_out: list[bytes] = []
    llm = StubLlm(chunk_delay_sec=0.0)
    tts = SlowCancelTts()
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
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: b"first" in pcm_out)
    barge_task = asyncio.create_task(orch.on_speech_start(source="vad"))
    await _wait_until(lambda: any(e.get("type") == "barge_in" for e in events))
    assert orch._downlink_muted
    await orch._on_tts_audio(b"LATE")
    assert b"LATE" not in pcm_out
    tts.allow_finish.set()
    await barge_task
    await orch.close()


def test_speech_start_barges_after_server_tts_end():
    asyncio.run(_speech_start_barges_after_server_tts_end())


async def _speech_start_barges_after_server_tts_end():
    orch, llm, _tts, events, pcm_out, _asr = _orch()
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: bool(pcm_out))
    await _wait_until(lambda: orch.state.phase == "idle", timeout=2.0)
    assert orch._client_may_have_tts
    barged = await orch.on_speech_start(source="vad")
    assert barged
    assert any(e.get("type") == "barge_in" for e in events)
    await orch.close()


def test_barge_cancels_in_flight_flush():
    asyncio.run(_barge_cancels_in_flight_flush())


async def _barge_cancels_in_flight_flush():
    class SlowFlushTts:
        def __init__(self) -> None:
            self.fed: list[str] = []
            self.flush_entered = asyncio.Event()
            self.flush_finished = False
            self.on_audio = None
            self.on_start = None
            self.on_end = None

        def bind(self, *, on_audio=None, on_start=None, on_end=None) -> None:
            if on_audio:
                self.on_audio = on_audio
            if on_start:
                self.on_start = on_start
            if on_end:
                self.on_end = on_end

        async def start(self, *, voice: str, sample_rate: int = 24000, gen_id: str = "") -> None:
            if self.on_start:
                result = self.on_start()
                if asyncio.iscoroutine(result):
                    await result

        async def feed(self, text: str) -> None:
            self.fed.append(text)
            if self.on_audio:
                result = self.on_audio(b"pcm")
                if asyncio.iscoroutine(result):
                    await result

        async def flush(self) -> None:
            self.flush_entered.set()
            await asyncio.sleep(2.0)
            self.flush_finished = True

        async def cancel(self) -> None:
            return

    events: list[dict] = []
    pcm_out: list[bytes] = []
    llm = StubLlm(chunk_delay_sec=0.0)
    tts = SlowFlushTts()
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
    await orch.on_speech_start(source="test")
    await orch.on_asr_partial("你好", False)
    await _wait_until(lambda: len(llm.calls) >= 1)
    await orch.on_eou(final_text="你好")
    await _wait_until(lambda: orch._llm_done)
    await _wait_until(lambda: tts.flush_entered.is_set())
    barged = await orch.on_speech_start(source="vad")
    assert barged
    await asyncio.sleep(0.05)
    assert not tts.flush_finished
    await orch.close()


def test_dashscope_urls_and_text_delta_filter():
    from backend.pipeline.config import PUBLIC_LLM_BASE, PUBLIC_TTS_WS, llm_base_url, tts_ws_url
    from backend.pipeline.adapters.llm_dashscope import build_responses_input, output_text_delta
    from backend.pipeline.dialogue import HistoryTurn

    assert llm_base_url(workspace_id="workspace-abc") == PUBLIC_LLM_BASE
    assert llm_base_url() == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert tts_ws_url(workspace_id="workspace-abc") == PUBLIC_TTS_WS
    assert llm_base_url(base_url="https://example.test/v1/") == "https://example.test/v1"
    legacy = (
        "https://ws.cn-beijing.maas.aliyuncs.com"
        "/api/v2/apps/protocols/compatible-mode/v1"
    )
    assert llm_base_url(base_url=legacy) == (
        "https://ws.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    from backend.pipeline.config import workspace_llm_base, workspace_tts_ws

    assert workspace_llm_base("ws") == (
        "https://ws.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert workspace_tts_ws("ws") == "wss://ws.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"

    items = build_responses_input(
        "系统",
        [HistoryTurn(role="user", text="上轮"), HistoryTurn(role="assistant", text="已播前缀")],
        "帮我查一下上海",
    )
    assert items[0] == {"role": "user", "content": "上轮"}
    assert items[-1] == {"role": "user", "content": "帮我查一下上海"}
    assert items[1]["role"] == "assistant"
    from backend.pipeline.dialogue import system_with_progress

    sys = system_with_progress("系统", [HistoryTurn(role="assistant", text="开场白")])
    assert "禁止再次朗读" in sys

    assert output_text_delta({"type": "response.output_text.delta", "delta": "好的"}) == "好的"
    assert output_text_delta({"type": "response.reasoning_summary_text.delta", "delta": "思考"}) == ""
    assert output_text_delta({"type": "response.thinking.delta", "delta": "x"}) == ""
