# -*- coding: utf-8 -*-
"""Resolve bench model strings to provider + credentials.

Model id formats:
  ark:doubao-seed-2-1-turbo-260628
  dashscope:qwen3.5-flash
  dashscope:qwen3.8-2.4t-a95b
  kimi:kimi-k2.6
  mimo:mimo-v2.5-pro
  maas:qwen3.8-flash              → workspace MaaS Responses host
  maas:qwen3.8-flash+thinking    → same + enable_thinking
  qwen3.5-flash                  → dashscope (default)
  kimi-k2.6                      → kimi (auto by prefix)
  mimo-v2.5-pro                  → mimo (auto by prefix)
  doubao-seed-2-1-turbo-260628   → ark (auto by prefix)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from backend.pipeline.adapters.llm_ark import DEFAULT_ARK_BASE, resolve_ark_service_tier
from backend.pipeline.adapters.llm_mimo import DEFAULT_MIMO_BASE, DEFAULT_MIMO_MODEL
from backend.pipeline.config import PipelineSettings, PUBLIC_LLM_BASE

DEFAULT_ARK_MODEL = "doubao-seed-2-1-turbo-260628"
DEFAULT_KIMI_BASE = "https://api.moonshot.cn/v1"
# Current Moonshot catalog (kimi-k2-turbo-preview may 404); override with KIMI_MODEL.
DEFAULT_KIMI_MODEL = "kimi-k2.6"

# Extra DashScope public models (same host/key as qwen3.5-flash; not workspace MaaS).
DEFAULT_DASHSCOPE_EXTRA_MODELS = (
    "qwen3.8-flash",
    "qwen3.7-flash",
    "qwen3.8-max",
    "qwen3.8-2.4t-a95b",
)

# These DashScope models reject /responses (or return empty); use chat.completions instead.
DASHSCOPE_CHAT_COMPLETIONS_MODELS = frozenset(
    {
        "qwen3.8-2.4t-a95b",
    }
)


@dataclass(frozen=True)
class ModelSpec:
    provider: str  # stub | dashscope | ark | maas | kimi | mimo
    model: str
    label: str  # original string shown in reports
    enable_thinking: bool = False


def load_ark_api_key() -> str:
    raw = (
        os.getenv("ARK_API_KEY")
        or os.getenv("VOLC_ARK_API_KEY")
        or os.getenv("DOUBAO_ARK_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    # OpenAI SDK adds Bearer; strip if user pasted full header value.
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def load_ark_base_url() -> str:
    return (os.getenv("ARK_BASE_URL") or os.getenv("VOLC_ARK_BASE_URL") or DEFAULT_ARK_BASE).strip().rstrip("/")


def load_kimi_api_key() -> str:
    raw = (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "").strip().strip('"').strip("'")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def load_kimi_base_url() -> str:
    return (
        os.getenv("KIMI_API_BASE") or os.getenv("MOONSHOT_API_BASE") or DEFAULT_KIMI_BASE
    ).strip().rstrip("/")


def load_kimi_default_model() -> str:
    return (os.getenv("KIMI_MODEL") or os.getenv("MOONSHOT_MODEL") or DEFAULT_KIMI_MODEL).strip() or DEFAULT_KIMI_MODEL


def load_mimo_api_key() -> str:
    raw = (os.getenv("MIMO_API_KEY") or os.getenv("XIAOMI_MIMO_API_KEY") or "").strip().strip('"').strip("'")
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def load_mimo_base_url() -> str:
    return (os.getenv("MIMO_BASE_URL") or os.getenv("MIMO_API_BASE") or DEFAULT_MIMO_BASE).strip().rstrip("/")


def load_mimo_default_model() -> str:
    return (os.getenv("MIMO_MODEL") or DEFAULT_MIMO_MODEL).strip() or DEFAULT_MIMO_MODEL


def load_maas_base_url() -> str:
    """Optional workspace override. Empty → use public DashScope like qwen3.5-flash."""
    raw = (os.getenv("BENCH_MAAS_BASE_URL") or os.getenv("DASHSCOPE_MAAS_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    legacy = "/api/v2/apps/protocols/compatible-mode/v1"
    if legacy in raw:
        raw = raw.replace(legacy, "/compatible-mode/v1")
        raw = raw.replace("/compatible-mode/v1/compatible-mode/v1", "/compatible-mode/v1")
    return raw.rstrip("/")


def load_maas_api_key(settings: PipelineSettings) -> str:
    return (
        os.getenv("BENCH_MAAS_API_KEY")
        or os.getenv("DASHSCOPE_MAAS_API_KEY")
        or settings.api_key
        or ""
    ).strip()


def _strip_thinking_flag(model: str) -> tuple[str, bool]:
    text = (model or "").strip()
    lower = text.lower()
    for suffix in ("+thinking", ":thinking", "/thinking"):
        if lower.endswith(suffix):
            return text[: -len(suffix)].strip(), True
    return text, False


def parse_model_spec(raw: str, *, default_provider: str = "dashscope") -> ModelSpec:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty model id")
    label = text
    provider = (default_provider or "dashscope").strip().lower()
    model = text
    thinking = False

    if ":" in text:
        left, right = text.split(":", 1)
        left_l = left.strip().lower()
        right = right.strip()
        if left_l in {"ark", "volc", "volcengine", "doubao_ark"} and right:
            model, thinking = _strip_thinking_flag(right)
            return ModelSpec(provider="ark", model=model, label=label, enable_thinking=thinking)
        if left_l in {"kimi", "moonshot", "ms"} and right:
            model, thinking = _strip_thinking_flag(right)
            return ModelSpec(provider="kimi", model=model, label=label, enable_thinking=thinking)
        if left_l in {"mimo", "xiaomi", "xiaomimimo"} and right:
            model, thinking = _strip_thinking_flag(right)
            return ModelSpec(provider="mimo", model=model, label=label, enable_thinking=thinking)
        if left_l in {"maas", "workspace", "pai_maas", "aliyun_maas"} and right:
            # Alias: same as dashscope public unless BENCH_MAAS_BASE_URL is set.
            model, thinking = _strip_thinking_flag(right)
            return ModelSpec(provider="maas", model=model, label=label, enable_thinking=thinking)
        if left_l in {"dashscope", "qwen", "bailian", "ds"} and right:
            model, thinking = _strip_thinking_flag(right)
            return ModelSpec(provider="dashscope", model=model, label=label, enable_thinking=thinking)
        if left_l in {"stub", "llm_stub"} and right:
            return ModelSpec(provider="stub", model=right or "stub", label=label)

    lower = text.lower()
    if lower.startswith("doubao-") or lower.startswith("ep-") or lower.startswith("seed-"):
        provider = "ark"
        model = text
    elif lower.startswith("kimi-") or lower.startswith("moonshot-"):
        provider = "kimi"
        model = text
    elif lower.startswith("mimo-"):
        provider = "mimo"
        model = text
    model, thinking = _strip_thinking_flag(model)
    return ModelSpec(provider=provider, model=model, label=label, enable_thinking=thinking)


def suggested_bench_models(settings: PipelineSettings) -> list[dict[str, str]]:
    """Presets for the /bench UI."""
    out: list[dict[str, str]] = [
        {
            "id": f"dashscope:{settings.llm_model}",
            "provider": "dashscope",
            "model": settings.llm_model,
            "label": f"DashScope {settings.llm_model}",
        },
    ]
    for name in DEFAULT_DASHSCOPE_EXTRA_MODELS:
        if name == settings.llm_model:
            continue
        out.append(
            {
                "id": f"dashscope:{name}",
                "provider": "dashscope",
                "model": name,
                "label": f"DashScope {name}",
            }
        )
    kimi_model = load_kimi_default_model()
    out.append(
        {
            "id": f"kimi:{kimi_model}",
            "provider": "kimi",
            "model": kimi_model,
            "label": f"Kimi {kimi_model}",
        }
    )
    mimo_model = load_mimo_default_model()
    out.append(
        {
            "id": f"mimo:{mimo_model}",
            "provider": "mimo",
            "model": mimo_model,
            "label": f"MiMo {mimo_model}",
        }
    )
    out.append(
        {
            "id": f"ark:{DEFAULT_ARK_MODEL}",
            "provider": "ark",
            "model": DEFAULT_ARK_MODEL,
            "label": f"Ark {DEFAULT_ARK_MODEL}",
        }
    )
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for item in out:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        uniq.append(item)
    return uniq


def provider_status(settings: PipelineSettings) -> dict[str, object]:
    maas_base = load_maas_base_url()
    return {
        "dashscope": {
            "has_api_key": bool(settings.api_key),
            "base_url": settings.resolved_llm_base or PUBLIC_LLM_BASE,
            "default_model": settings.llm_model,
            "extra_models": list(DEFAULT_DASHSCOPE_EXTRA_MODELS),
        },
        "kimi": {
            "has_api_key": bool(load_kimi_api_key()),
            "base_url": load_kimi_base_url(),
            "default_model": load_kimi_default_model(),
        },
        "mimo": {
            "has_api_key": bool(load_mimo_api_key()),
            "base_url": load_mimo_base_url(),
            "default_model": load_mimo_default_model(),
        },
        "ark": {
            "has_api_key": bool(load_ark_api_key()),
            "base_url": load_ark_base_url(),
            "default_model": DEFAULT_ARK_MODEL,
            "service_tier": resolve_ark_service_tier() or "off",
        },
        "maas": {
            # Optional override only; empty base_url means same as public dashscope.
            "has_api_key": bool(load_maas_api_key(settings)),
            "base_url": maas_base or (settings.resolved_llm_base or PUBLIC_LLM_BASE),
            "uses_public_dashscope": not bool(maas_base),
        },
        "stub": {"has_api_key": True, "base_url": "", "default_model": "stub"},
    }


def assert_keys_for_models(specs: list[ModelSpec], settings: PipelineSettings, *, need_tts: bool) -> None:
    if settings.llm_adapter in {"stub", "llm_stub"}:
        return
    providers = {s.provider for s in specs}
    if "dashscope" in providers and not settings.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY / PIPELINE_API_KEY required for dashscope models")
    if "maas" in providers and not load_maas_api_key(settings):
        raise RuntimeError("DASHSCOPE_API_KEY required for maas aliases (same as dashscope public)")
    if "kimi" in providers and not load_kimi_api_key():
        raise RuntimeError("KIMI_API_KEY required for kimi models (Moonshot)")
    if "mimo" in providers and not load_mimo_api_key():
        raise RuntimeError("MIMO_API_KEY required for mimo models (Xiaomi MiMo)")
    if "ark" in providers and not load_ark_api_key():
        raise RuntimeError("ARK_API_KEY required for ark models (Volcengine Ark)")
    if need_tts and settings.tts_adapter not in {"stub", "tts_stub"} and not settings.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY required for real TTS (or use --llm-only / stub)")
