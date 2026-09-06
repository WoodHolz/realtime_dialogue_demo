# -*- coding: utf-8 -*-
"""Same prompt stack as /t2sbg-pipeline (verification + optional TTS manuscript rules)."""
from __future__ import annotations

from backend.prompt_manager import PromptManager
from config.supervisor_audit_prompt import with_supervisor_audit
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
    supervisor_audit: bool = False,
) -> tuple[str, str]:
    """Return (system_text, source_label)."""
    if (system_override or "").strip():
        text = system_override.strip()
        src = "custom"
    elif no_tts_prompt:
        text = load_pipeline_system_prompt(include_tts_script=False)
        src = "pipeline verification (no TTS block)"
    else:
        text = load_pipeline_system_prompt(include_tts_script=True)
        src = "pipeline verification + TTS_SCRIPT_REQUIREMENTS"

    if supervisor_audit:
        text = with_supervisor_audit(text)
        src = f"{src} + SUPERVISOR_AUDIT"
    return text, src
