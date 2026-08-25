# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HistoryTurn:
    role: str
    text: str


OPENING_DONE_HINT = (
    "【进度约束】开场白已经完成，禁止再次朗读、改写或重复开场白。"
    "请根据对话历史按核实问题清单继续下一题；对方表示方便即进入 Q1（确认主体公司）。"
)


def system_with_progress(system: str, history: list[HistoryTurn]) -> str:
    """Keep the full script, but stop the model from reciting the opening every turn."""
    base = (system or "").strip()
    if any((t.text or "").strip() for t in history if t.role == "assistant"):
        return f"{base}\n\n{OPENING_DONE_HINT}" if base else OPENING_DONE_HINT
    return base


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
