# -*- coding: utf-8 -*-
"""User-prompt alignment and sentence splitting for the split pipeline."""
from __future__ import annotations

import re

_TRAILING_PUNCT = re.compile(r"[\s。！？；，、,.!?;:：]+$")
_WHITESPACE = re.compile(r"\s+")
_SENTENCE_END = frozenset("。！？；\n")
_LATIN_END = frozenset(".!?")


def normalize_user_text(text: str) -> str:
    """Strip whitespace/trailing punctuation so '吧。' matches '吧'."""
    folded = _WHITESPACE.sub("", (text or "").strip())
    return _TRAILING_PUNCT.sub("", folded)


def user_prompts_aligned(prompt_user: str, final_user: str) -> bool:
    """True only when prompts are equal after normalize. Prefix growth is NOT aligned."""
    left = normalize_user_text(prompt_user)
    right = normalize_user_text(final_user)
    if not left or not right:
        return False
    return left == right


class SentenceBuffer:
    """Accumulate LLM token deltas and emit completed sentences."""

    def __init__(self) -> None:
        self._buf = ""

    def clear(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        if not delta:
            return []
        self._buf += delta
        out: list[str] = []
        start = 0
        for i, ch in enumerate(self._buf):
            ended = ch in _SENTENCE_END
            if not ended and ch in _LATIN_END:
                nxt = self._buf[i + 1] if i + 1 < len(self._buf) else ""
                ended = (not nxt) or nxt.isspace() or nxt in _SENTENCE_END
            if ended:
                sent = self._buf[start : i + 1].strip()
                if sent:
                    out.append(sent)
                start = i + 1
        self._buf = self._buf[start:]
        return out

    def flush(self) -> str:
        leftover = self._buf.strip()
        self._buf = ""
        return leftover
