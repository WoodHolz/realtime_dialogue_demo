# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HistoryTurn:
    role: str
    text: str


@dataclass
class DialogueState:
    """Orchestrator-owned conversation memory. Speculative drafts never live here."""

    history: list[HistoryTurn] = field(default_factory=list)
    user_snapshot: str = ""
    prompt_user: str = ""
    gen_id: str = ""
    turn_id: str = ""
    spoken_text: str = ""
    draft: str = ""
    phase: str = "idle"
    pending_user: str = ""

    def committed_history(self) -> list[HistoryTurn]:
        return list(self.history)
