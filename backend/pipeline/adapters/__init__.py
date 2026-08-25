# -*- coding: utf-8 -*-
from __future__ import annotations

from backend.pipeline.adapters.asr_volc import VolcAsrAdapter
from backend.pipeline.adapters.llm_dashscope import DashScopeResponsesLlm
from backend.pipeline.adapters.llm_stub import StubLlm
from backend.pipeline.adapters.tts_dashscope import DashScopeSpeechTts
from backend.pipeline.adapters.tts_stub import StubTts

__all__ = [
    "VolcAsrAdapter",
    "DashScopeResponsesLlm",
    "StubLlm",
    "DashScopeSpeechTts",
    "StubTts",
]
