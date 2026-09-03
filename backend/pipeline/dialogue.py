# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HistoryTurn:
    role: str
    text: str


OPENING_DONE_HINT = (
    "【进度约束】开场白已经完成，禁止再次朗读、改写或重复开场白。"
    "请严格按必采顺序推进下一未完成字段；对方已明确「对/对的/是的」后禁止复读同一问。"
    "竞业/保密必须开放询问有无；对方说没有即达标，禁止用任务卡「有签署」覆盖或收线后再翻案。"
    "说出结束语后立即停止，禁止长总结、禁止再核对签署。"
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
