# -*- coding: utf-8 -*-
"""
Dynamic prompt management: stage-based system prompts and semi mode injection.
"""
from __future__ import annotations

from typing import Any, Mapping

from config.prompt_templates import (
    DEFAULT_SPEAKING_STYLE,
    DEFAULT_SYSTEM_ROLE,
    DEFAULT_VERIFICATION_CASE,
    STAGE_PROMPTS,
    build_verification_system_role,
    merge_verification_case,
    wrap_asr_for_semi,
)


class PromptManager:
    """Manages current stage and system prompt for the interviewer."""

    def __init__(self):
        self._stage = "introduction"
        self._custom_role: str | None = None
        self._custom_style: str | None = None
        self._verification_case: dict[str, str] = dict(DEFAULT_VERIFICATION_CASE)

    @property
    def stage(self) -> str:
        return self._stage

    def set_stage(self, stage: str) -> None:
        if stage in STAGE_PROMPTS:
            self._stage = stage

    def get_verification_case(self) -> dict[str, str]:
        return dict(self._verification_case)

    def set_verification_case(
        self,
        case: Mapping[str, Any] | None = None,
        *,
        rebuild_system_role: bool = True,
    ) -> str | None:
        """Merge external case slots; optionally rebuild system_role from template."""
        self._verification_case = merge_verification_case(
            case,
            base=self._verification_case or DEFAULT_VERIFICATION_CASE,
        )
        if not rebuild_system_role:
            return None
        role = build_verification_system_role(self._verification_case)
        self._custom_role = role
        return role

    def get_system_role(self) -> str:
        if self._custom_role is not None:
            return self._custom_role
        return DEFAULT_SYSTEM_ROLE

    def get_speaking_style(self) -> str:
        if self._custom_style is not None:
            return self._custom_style
        return DEFAULT_SPEAKING_STYLE

    def set_custom_prompt(
        self,
        system_role: str | None = None,
        speaking_style: str | None = None,
        verification_case: Mapping[str, Any] | None = None,
    ) -> None:
        if verification_case is not None:
            # Case wins over a raw system_role paste when both are sent:
            # rebuild from template so each candidate stays consistent.
            self.set_verification_case(verification_case, rebuild_system_role=True)
            if speaking_style is not None:
                self._custom_style = speaking_style
            return
        self._custom_role = system_role
        self._custom_style = speaking_style

    def inject_for_semi(self, asr_text: str) -> str:
        """Return text to send as ChatTextQuery in semi mode (ASR + follow-up hint)."""
        return wrap_asr_for_semi(asr_text)
