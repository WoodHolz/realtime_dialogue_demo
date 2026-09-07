# -*- coding: utf-8 -*-
"""Model registry for eot_bench (LiveKit Turn Detector v1 / v1-mini)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

ModelKind = Literal["streaming", "batch"]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: ModelKind
    adapter_ref: str
    display_name: str
    score_point: float = 0.2
    requires_env: tuple[str, ...] = ()

    def missing_env(self) -> list[str]:
        return [k for k in self.requires_env if not (os.environ.get(k) or "").strip()]


MODELS: dict[str, ModelSpec] = {
    "v1": ModelSpec(
        id="v1",
        kind="streaming",
        adapter_ref="eot_harness.livekit_turn_detector_adapter:LiveKitTurnDetectorAdapter",
        display_name="LiveKit Turn Detector v1",
        score_point=0.2,
        requires_env=("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
    ),
    "v1-mini": ModelSpec(
        id="v1-mini",
        kind="batch",
        adapter_ref="eot_harness.livekit_turn_detector_mini_adapter:LiveKitTurnDetectorMiniAdapter",
        display_name="LiveKit Turn Detector v1-mini",
        score_point=0.2,
    ),
}

ALIASES = {
    "livekit-v1": "v1",
    "livekit_v1": "v1",
    "turn-detector-v1": "v1",
    "mini": "v1-mini",
    "livekit-v1-mini": "v1-mini",
    "livekit_v1_mini": "v1-mini",
    "turn-detector-v1-mini": "v1-mini",
}


def resolve_model_id(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        raise ValueError("model id is empty")
    key = ALIASES.get(key, key)
    if key not in MODELS:
        known = ", ".join(sorted(MODELS))
        raise ValueError(f"unknown model {raw!r}; expected one of: {known}")
    return key


def parse_models(raw: str | list[str] | None) -> list[ModelSpec]:
    if raw is None or raw == "" or raw == []:
        return [MODELS["v1"]]
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in raw if str(p).strip()]
    if not parts:
        return [MODELS["v1"]]
    out: list[ModelSpec] = []
    seen: set[str] = set()
    for part in parts:
        mid = resolve_model_id(part)
        if mid in seen:
            continue
        seen.add(mid)
        out.append(MODELS[mid])
    return out


def assert_keys_for_models(models: list[ModelSpec]) -> None:
    missing: list[str] = []
    for spec in models:
        for key in spec.missing_env():
            missing.append(f"{spec.id}:{key}")
    if missing:
        raise RuntimeError(
            "missing credentials for eot_bench models: "
            + ", ".join(missing)
            + " (set in .env / environment)"
        )


def model_status() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in MODELS.values():
        miss = spec.missing_env()
        rows.append(
            {
                "id": spec.id,
                "display_name": spec.display_name,
                "kind": spec.kind,
                "adapter": spec.adapter_ref,
                "ready": not miss,
                "missing_env": miss,
            }
        )
    return rows
