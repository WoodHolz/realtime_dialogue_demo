# -*- coding: utf-8 -*-
"""
Dynamic prompt management: stage-based system prompts and semi mode injection.
"""
from __future__ import annotations

from config.prompt_templates import (
    DEFAULT_SYSTEM_ROLE,
    DEFAULT_SPEAKING_STYLE,
    wrap_asr_for_semi,
    STAGE_PROMPTS,
)


class PromptManager:
    """Manages current stage and system prompt for the interviewer."""

    def __init__(self):
        self._stage = "introduction"
        self._custom_role: str | None = None
        self._custom_style: str | None = None

    @property
    def stage(self) -> str:
        return self._stage

    def set_stage(self, stage: str) -> None:
        if stage in STAGE_PROMPTS:
            self._stage = stage

    def get_system_role(self) -> str:
        if self._custom_role is not None:
            return self._custom_role
        return DEFAULT_SYSTEM_ROLE

    def get_speaking_style(self) -> str:
        if self._custom_style is not None:
            return self._custom_style
        return DEFAULT_SPEAKING_STYLE

    def set_custom_prompt(self, system_role: str | None = None, speaking_style: str | None = None) -> None:
        self._custom_role = system_role
        self._custom_style = speaking_style

    def inject_for_semi(self, asr_text: str) -> str:
        """Return text to send as ChatTextQuery in semi mode (ASR + follow-up hint)."""
        return wrap_asr_for_semi(asr_text)
