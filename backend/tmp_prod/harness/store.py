# -*- coding: utf-8 -*-
"""JSON-backed NextAction store for tmp-prod harness."""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tmp_prod.harness.models import (
    STATUS_PENDING,
    STATUS_RUNNING,
    NextAction,
    action_from_dict,
)

LOG = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_root() -> Path:
    return Path(os.environ.get("TMP_PROD_ROOT", "data/tmp_prod"))


class ActionStore:
    """Single-file action queue under data/tmp_prod/harness/actions.json."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_root()) / "harness"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "actions.json"
        self._lock = threading.Lock()
        self._actions: dict[str, NextAction] = {}
        self._mtime_ns: int = -1
        self._load()

    def _file_mtime_ns(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return -1

    def _load(self) -> None:
        if not self.path.is_file():
            self._actions = {}
            self._mtime_ns = -1
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else (raw.get("actions") or [])
            loaded: dict[str, NextAction] = {}
            for item in items:
                action = action_from_dict(item if isinstance(item, dict) else None)
                if action:
                    loaded[action.action_id] = action
            self._actions = loaded
            self._mtime_ns = self._file_mtime_ns()
        except Exception:
            LOG.exception("tmp-prod harness action store load failed path=%s", self.path)

    def _reload_if_stale_unlocked(self) -> None:
        """Re-read disk so scheduler sees actions written by another request/worker."""
        mtime = self._file_mtime_ns()
        if mtime != self._mtime_ns:
            self._load()

    def _save_unlocked(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = [a.to_dict() for a in sorted(self._actions.values(), key=lambda x: x.created_at)]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._mtime_ns = self._file_mtime_ns()

    def save(self, action: NextAction) -> NextAction:
        action.updated_at = _utcnow()
        with self._lock:
            self._reload_if_stale_unlocked()
            self._actions[action.action_id] = action
            self._save_unlocked()
        return action

    def get(self, action_id: str) -> NextAction | None:
        with self._lock:
            self._reload_if_stale_unlocked()
            return self._actions.get(action_id)

    def list_actions(
        self,
        *,
        batch_id: str | None = None,
        contact_id: str | None = None,
        status: str | None = None,
    ) -> list[NextAction]:
        with self._lock:
            self._reload_if_stale_unlocked()
            out: list[NextAction] = []
            for a in self._actions.values():
                if batch_id and a.batch_id != batch_id:
                    continue
                if contact_id and a.contact_id != contact_id:
                    continue
                if status and a.status != status:
                    continue
                out.append(a)
            return sorted(out, key=lambda x: x.due_at)

    def create(
        self,
        *,
        batch_id: str,
        contact_id: str,
        type: str,
        due_at: str,
        reason: str = "",
        source_call_id: str = "",
        dial_options: dict[str, Any] | None = None,
        callback_context: dict[str, Any] | None = None,
        status: str = STATUS_PENDING,
    ) -> NextAction:
        action = NextAction(
            action_id=uuid.uuid4().hex[:12],
            batch_id=batch_id,
            contact_id=contact_id,
            type=type,
            due_at=due_at,
            status=status,
            reason=reason,
            source_call_id=source_call_id or "",
            dial_options=dict(dial_options or {}),
            callback_context=dict(callback_context or {}),
        )
        return self.save(action)

    def claim_due(self, *, now: datetime | None = None) -> list[NextAction]:
        """Atomically move due pending actions to running; return claimed list."""
        now = now or datetime.now(timezone.utc)
        claimed: list[NextAction] = []
        with self._lock:
            self._reload_if_stale_unlocked()
            for action in list(self._actions.values()):
                if action.status != STATUS_PENDING:
                    continue
                due = _parse_iso(action.due_at)
                if due is None or due > now:
                    continue
                action.status = STATUS_RUNNING
                action.updated_at = _utcnow()
                self._actions[action.action_id] = action
                claimed.append(action)
            if claimed:
                self._save_unlocked()
        return claimed

    def mark(
        self,
        action_id: str,
        *,
        status: str,
        error: str = "",
    ) -> NextAction | None:
        with self._lock:
            self._reload_if_stale_unlocked()
            action = self._actions.get(action_id)
            if not action:
                return None
            action.status = status
            if error:
                action.error = error
            action.updated_at = _utcnow()
            self._save_unlocked()
            return action

    def cancel_pending_for_contact(self, batch_id: str, contact_id: str) -> int:
        """Cancel pending actions when contact is terminal or re-scheduled."""
        n = 0
        with self._lock:
            self._reload_if_stale_unlocked()
            for action in self._actions.values():
                if (
                    action.batch_id == batch_id
                    and action.contact_id == contact_id
                    and action.status == STATUS_PENDING
                ):
                    action.status = "cancelled"
                    action.updated_at = _utcnow()
                    n += 1
            if n:
                self._save_unlocked()
        return n

    def reset_running_to_pending(self) -> int:
        """On process start: reclaim interrupted running actions."""
        n = 0
        with self._lock:
            self._reload_if_stale_unlocked()
            for action in self._actions.values():
                if action.status == STATUS_RUNNING:
                    action.status = STATUS_PENDING
                    action.updated_at = _utcnow()
                    n += 1
            if n:
                self._save_unlocked()
        return n


def _parse_iso(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_STORE: ActionStore | None = None


def get_action_store() -> ActionStore:
    global _STORE
    if _STORE is None:
        _STORE = ActionStore()
    return _STORE
