from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

VadEventKind = Literal["speech_start", "speech_continue", "turn_end_candidate", "silence"]

# Providers that use semantic turn-end debounce in server._t2s_trigger_complete.
SEMANTIC_VAD_SOURCES = frozenset({"duplug", "smart_turn_v3"})


@dataclass(frozen=True)
class VadEvent:
    kind: VadEventKind
    provider: str
    state: str
    meta: dict[str, Any] = field(default_factory=dict)
