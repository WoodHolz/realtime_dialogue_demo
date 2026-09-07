# -*- coding: utf-8 -*-
"""Prompt stacks for bench: fast path vs standalone supervisor."""
from __future__ import annotations

from backend.prompt_manager import PromptManager
from config.supervisor_audit_prompt import SUPERVISOR_SYSTEM_PROMPT, with_supervisor_audit
from config.tts_script_prompt import with_tts_script_requirements

DEFAULT_QUERY = "方便，你说。"

# recovery 智力评测默认：独立 Supervisor（不与快模型流程稿拼接）
PROMPT_MODE_SUPERVISOR = "supervisor"
PROMPT_MODE_FAST = "fast"
PROMPT_MODE_FAST_AUDIT = "fast_audit"  # legacy concat A/B


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
    prompt_mode: str = "",
) -> tuple[str, str]:
    """Return (system_text, source_label).

    prompt_mode:
      supervisor  — standalone SUPERVISOR_SYSTEM_PROMPT (recovery default)
      fast        — pipeline verification + TTS manuscript
      fast_audit  — fast + SUPERVISOR_AUDIT concat (legacy)
    If prompt_mode empty: honor supervisor_audit flag for backward compat
    (True → fast_audit, False → fast).
    """
    mode = (prompt_mode or "").strip().lower()
    if not mode:
        mode = PROMPT_MODE_FAST_AUDIT if supervisor_audit else PROMPT_MODE_FAST

    if (system_override or "").strip():
        return system_override.strip(), "custom"

    if mode == PROMPT_MODE_SUPERVISOR:
        return SUPERVISOR_SYSTEM_PROMPT.strip(), "SUPERVISOR_SYSTEM (standalone)"

    if mode == PROMPT_MODE_FAST_AUDIT:
        text = load_pipeline_system_prompt(include_tts_script=not no_tts_prompt)
        text = with_supervisor_audit(text)
        src = (
            "pipeline verification + SUPERVISOR_AUDIT"
            if no_tts_prompt
            else "pipeline verification + TTS_SCRIPT_REQUIREMENTS + SUPERVISOR_AUDIT"
        )
        return text, src

    # fast
    if no_tts_prompt:
        return load_pipeline_system_prompt(include_tts_script=False), "pipeline verification (no TTS block)"
    return (
        load_pipeline_system_prompt(include_tts_script=True),
        "pipeline verification + TTS_SCRIPT_REQUIREMENTS",
    )
