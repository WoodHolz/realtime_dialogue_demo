from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

HG_MODEL = "livekit/turn-detector"
ONNX_FILENAME = "model_q8.onnx"
MODEL_REVISION = "v0.4.1-intl"
MAX_HISTORY_TOKENS = 128
MAX_HISTORY_TURNS = 6

_EOU_MODEL: LivekitEouModel | None = None


def livekit_turn_model_dir() -> Path | None:
    """Optional local snapshot of livekit/turn-detector (offline / mirror download)."""
    env = (os.environ.get("LIVEKIT_TURN_MODEL_DIR", "") or "").strip()
    if not env:
        return None
    path = Path(env).expanduser().resolve()
    return path if path.is_dir() else None


def _local_asset_path(filename: str, *, subfolder: str | None = None) -> Path | None:
    root = livekit_turn_model_dir()
    if root is None:
        return None
    if subfolder:
        candidate = root / subfolder / filename
    else:
        candidate = root / filename
    return candidate if candidate.is_file() else None


def _download_from_hf(filename: str, *, subfolder: str | None = None) -> str:
    local = _local_asset_path(filename, subfolder=subfolder)
    if local is not None:
        LOG.info("Using local LiveKit turn-detector asset: %s", local)
        return str(local)

    from huggingface_hub import hf_hub_download

    local_only = os.environ.get("LIVEKIT_TURN_LOCAL_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    kwargs: dict[str, Any] = {
        "repo_id": HG_MODEL,
        "filename": filename,
        "revision": MODEL_REVISION,
    }
    if subfolder:
        kwargs["subfolder"] = subfolder
    if local_only:
        kwargs["local_files_only"] = True

    endpoint = (os.environ.get("HF_ENDPOINT", "") or os.environ.get("HUGGINGFACE_HUB_ENDPOINT", "") or "").strip()
    if endpoint:
        kwargs["endpoint"] = endpoint.rstrip("/")

    try:
        return hf_hub_download(**kwargs)
    except Exception as exc:
        hint = (
            "HuggingFace 下载 livekit/turn-detector 失败（网络超时常见）。"
            "可尝试：1) export HF_ENDPOINT=https://hf-mirror.com "
            "2) bash scripts/download_livekit_turn_detector.sh "
            "3) 下载到本地后设置 LIVEKIT_TURN_MODEL_DIR=/path/to/turn-detector"
        )
        raise RuntimeError(f"{hint} Original error: {exc}") from exc


class LivekitEouModel:
    """LiveKit multilingual turn-detector (ONNX INT8 on CPU)."""

    def __init__(self) -> None:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        onnx_path = _download_from_hf(ONNX_FILENAME, subfolder="onnx")
        languages_path = _download_from_hf("languages.json")
        with Path(languages_path).open("r", encoding="utf-8") as f:
            self._languages: dict[str, dict[str, float]] = json.load(f)

        cpu_count = os.cpu_count() or 2
        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = max(1, min(math.ceil(cpu_count / 2), 4))
        sess_options.inter_op_num_threads = 1
        sess_options.add_session_config_entry("session.dynamic_block_base", "4")
        self._session = ort.InferenceSession(
            onnx_path,
            providers=["CPUExecutionProvider"],
            sess_options=sess_options,
        )
        local_root = livekit_turn_model_dir()
        if local_root is not None:
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(local_root),
                local_files_only=True,
                truncation_side="left",
            )
        else:
            tok_kwargs: dict[str, Any] = {
                "revision": MODEL_REVISION,
                "truncation_side": "left",
            }
            endpoint = (os.environ.get("HF_ENDPOINT", "") or os.environ.get("HUGGINGFACE_HUB_ENDPOINT", "") or "").strip()
            if endpoint:
                tok_kwargs["endpoint"] = endpoint.rstrip("/")
            self._tokenizer = AutoTokenizer.from_pretrained(HG_MODEL, **tok_kwargs)

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not text:
            return ""
        text = unicodedata.normalize("NFKC", text.lower())
        text = "".join(
            ch
            for ch in text
            if not (unicodedata.category(ch).startswith("P") and ch not in ["'", "-"])
        )
        return re.sub(r"\s+", " ", text).strip()

    def _format_chat_ctx(self, chat_ctx: list[dict[str, str]]) -> str:
        new_chat_ctx: list[dict[str, str]] = []
        last_msg: dict[str, str] | None = None
        for msg in chat_ctx:
            content = self._normalize_text(msg.get("content", ""))
            if not content:
                continue
            if last_msg and last_msg["role"] == msg["role"]:
                last_msg["content"] += f" {content}"
            else:
                new_chat_ctx.append({"role": msg["role"], "content": content})
                last_msg = new_chat_ctx[-1]

        convo_text = self._tokenizer.apply_chat_template(
            new_chat_ctx,
            add_generation_prompt=False,
            add_special_tokens=False,
            tokenize=False,
        )
        marker = "<|im_end|>"
        ix = convo_text.rfind(marker)
        return convo_text[:ix] if ix >= 0 else convo_text

    def threshold_for_language(self, language: str | None, *, override: float | None = None) -> float:
        if override is not None:
            return override
        lang = (language or "zh").strip().lower().replace("_", "-")
        base = lang.split("-", 1)[0]
        lang_data = self._languages.get(lang) or self._languages.get(base)
        if lang_data is None:
            return 0.5
        return float(lang_data["threshold"])

    def predict_eou_probability(
        self,
        chat_ctx: list[dict[str, str]],
    ) -> tuple[float, str]:
        """Return (probability, formatted_input_text)."""
        text = self._format_chat_ctx(chat_ctx)
        if not text.strip():
            return 0.0, text

        inputs = self._tokenizer(
            text,
            add_special_tokens=False,
            return_tensors="np",
            max_length=MAX_HISTORY_TOKENS,
            truncation=True,
        )
        outputs = self._session.run(None, {"input_ids": inputs["input_ids"].astype("int64")})
        prob = float(outputs[0].flatten()[-1])
        return prob, text


def get_livekit_eou_model() -> LivekitEouModel:
    global _EOU_MODEL
    if _EOU_MODEL is None:
        LOG.info("Loading LiveKit turn-detector model (%s @ %s)", HG_MODEL, MODEL_REVISION)
        _EOU_MODEL = LivekitEouModel()
    return _EOU_MODEL


def build_chat_ctx(
    *,
    user_text: str,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if history:
        for msg in history:
            role = msg.get("role", "")
            content = (msg.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
    user_text = (user_text or "").strip()
    if user_text:
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = f"{messages[-1]['content']} {user_text}".strip()
        else:
            messages.append({"role": "user", "content": user_text})
    return messages[-MAX_HISTORY_TURNS:]
