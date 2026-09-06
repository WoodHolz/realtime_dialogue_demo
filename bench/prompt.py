# -*- coding: utf-8 -*-
"""Same prompt stack as /t2sbg-pipeline (verification + optional TTS manuscript rules)."""
from __future__ import annotations

from backend.prompt_manager import PromptManager
from config.tts_script_prompt import with_tts_script_requirements

DEFAULT_QUERY = "方便，你说。"


def load_pipeline_system_prompt(*, include_tts_script: bool = True) -> str:
    role = PromptManager().get_system_role()
    if include_tts_script:
        return with_tts_script_requirements(role)
    return role


def resolve_system_prompt(
    *,
    system_override: str = "",
    no_tts_prompt: bool = False,
) -> tuple[str, str]:
    """Return (system_text, source_label)."""
    if (system_override or "").strip():
        return system_override.strip(), "custom"
    if no_tts_prompt:
        return load_pipeline_system_prompt(include_tts_script=False), "pipeline verification (no TTS block)"
    return load_pipeline_system_prompt(include_tts_script=True), "pipeline verification + TTS_SCRIPT_REQUIREMENTS"
