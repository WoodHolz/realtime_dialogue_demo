# -*- coding: utf-8 -*-
from __future__ import annotations

from backend.pipeline.adapters.asr_volc import VolcAsrAdapter
from backend.pipeline.adapters.llm_stub import StubLlm
from backend.pipeline.adapters.tts_stub import StubTts

__all__ = ["VolcAsrAdapter", "StubLlm", "StubTts"]
