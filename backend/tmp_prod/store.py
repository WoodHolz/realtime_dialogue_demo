# -*- coding: utf-8 -*-
"""Persistent JSON store for tmp-prod demo batches."""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_root() -> Path:
    return Path("data/tmp_prod")


@dataclass
class ContactJob:
    contact_id: str
    employer: str
    name: str
    gender: str
    phone: str
    status: str = "pending"  # pending|dialing|in_call|waiting_retry|completed|failed
    result: str = ""
    result_summary: str = ""
    fail_reason: str = ""
    attempt: int = 0
    max_attempts: int = 2
    last_call_id: str = ""
    call_ids: list[str] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    recordings: dict[str, str] = field(default_factory=dict)
    recording_dir: str = ""
    stereo_path: str = ""  # L=user R=AI dual-channel WAV (aligned under batch)
    promised_time: str = ""
    email: str = ""
    next_retry_at: str = ""
    updated_at: str = field(default_factory=_utcnow)
    created_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def contact_from_dict(data: dict[str, Any]) -> ContactJob:
    known = {f.name for f in fields(ContactJob)}
    return ContactJob(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Batch:
    batch_id: str
    source_name: str = ""
    status: str = "created"  # created|running|done
    contacts: list[ContactJob] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_name": self.source_name,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "contacts": [c.to_dict() for c in self.contacts],
            "stats": self.stats(),
        }

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.contacts:
            counts[c.status] = counts.get(c.status, 0) + 1
        counts["total"] = len(self.contacts)
        return counts


class BatchStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._batches: dict[str, Batch] = {}
        self._load_all()

    def _path(self, batch_id: str) -> Path:
        return self.root / batch_id / "batch.json"

    def _load_all(self) -> None:
        for p in self.root.glob("*/batch.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                contacts = [contact_from_dict(c) for c in data.get("contacts") or []]
                b = Batch(
                    batch_id=data["batch_id"],
                    source_name=data.get("source_name") or "",
                    status=data.get("status") or "created",
                    contacts=contacts,
                    created_at=data.get("created_at") or _utcnow(),
                    updated_at=data.get("updated_at") or _utcnow(),
                )
                self._batches[b.batch_id] = b
            except Exception:
                continue

    def save(self, batch: Batch) -> None:
        batch.updated_at = _utcnow()
        path = self._path(batch.batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._batches[batch.batch_id] = batch
            path.write_text(
                json.dumps(batch.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def list_batches(self) -> list[Batch]:
        return sorted(self._batches.values(), key=lambda b: b.created_at, reverse=True)

    def create_batch(self, contacts: list[dict[str, str]], *, source_name: str = "") -> Batch:
        batch_id = uuid.uuid4().hex[:12]
        jobs = [
            ContactJob(
                contact_id=uuid.uuid4().hex[:10],
                employer=c.get("employer") or "",
                name=c.get("name") or "",
                gender=c.get("gender") or "",
                phone=c.get("phone") or "",
            )
            for c in contacts
        ]
        batch = Batch(batch_id=batch_id, source_name=source_name, contacts=jobs)
        self.save(batch)
        return batch

    def update_contact(self, batch_id: str, contact_id: str, **fields: Any) -> ContactJob | None:
        batch = self.get(batch_id)
        if not batch:
            return None
        for c in batch.contacts:
            if c.contact_id == contact_id:
                for k, v in fields.items():
                    if hasattr(c, k):
                        setattr(c, k, v)
                c.updated_at = _utcnow()
                self.save(batch)
                return c
        return None


_STORE: BatchStore | None = None


def get_batch_store() -> BatchStore:
    global _STORE
    if _STORE is None:
        _STORE = BatchStore()
    return _STORE
