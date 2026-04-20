from __future__ import annotations

"""
Client for calling an external OpenAPI-style chat model to generate follow-up questions
from ASR text.

The API is assumed to be OpenAI-compatible:
- POST {api_base}/v1/chat/completions
- Bearer token in Authorization header
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from config.prompt_templates import DEFAULT_SYSTEM_ROLE


LOG = logging.getLogger(__name__)


def _parse_extra_body(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("followup: invalid FOLLOWUP_EXTRA_BODY JSON, ignored")
    return {}


def _merge_followup_block_from_env(active: str, block: dict[str, Any]) -> dict[str, Any]:
    """Fill empty api_base / api_key / model from provider-specific env vars."""
    b = dict(block)
    if active == "kimi":
        if not str(b.get("api_base") or "").strip():
            b["api_base"] = os.getenv("KIMI_API_BASE", "") or ""
        if not str(b.get("api_key") or "").strip():
            b["api_key"] = os.getenv("KIMI_API_KEY", "") or ""
        if not str(b.get("model") or "").strip():
            b["model"] = os.getenv("KIMI_MODEL", "") or ""
    elif active == "eas":
        if not str(b.get("api_base") or "").strip():
            b["api_base"] = os.getenv("EAS_API_BASE", "") or ""
        if not str(b.get("api_key") or "").strip():
            b["api_key"] = os.getenv("EAS_API_KEY", "") or ""
        if not str(b.get("model") or "").strip():
            b["model"] = os.getenv("EAS_MODEL", "") or ""
        eas_extra = os.getenv("EAS_EXTRA_BODY")
        if eas_extra and str(eas_extra).strip():
            parsed = _parse_extra_body(eas_extra)
            if parsed:
                file_extra = _parse_extra_body(b.get("extra_body"))
                b["extra_body"] = {**file_extra, **parsed}
    elif active == "bailian":
        if not str(b.get("api_base") or "").strip():
            b["api_base"] = os.getenv("DASHSCOPE_API_BASE", "") or ""
        if not str(b.get("api_key") or "").strip():
            b["api_key"] = os.getenv("DASHSCOPE_API_KEY", "") or ""
        if not str(b.get("model") or "").strip():
            b["model"] = os.getenv("DASHSCOPE_MODEL", "") or ""
        bailian_extra = os.getenv("DASHSCOPE_EXTRA_BODY")
        if bailian_extra and str(bailian_extra).strip():
            parsed = _parse_extra_body(bailian_extra)
            if parsed:
                file_extra = _parse_extra_body(b.get("extra_body"))
                b["extra_body"] = {**file_extra, **parsed}
    return b


def _followup_block_from_config(block: dict[str, Any]) -> dict[str, Any]:
    if not block.get("enabled", True):
        return {}
    return {
        "api_base": (block.get("api_base") or "").strip().rstrip("/"),
        "api_key": (block.get("api_key") or "").strip(),
        "model": (block.get("model") or "").strip(),
        "extra_body": _parse_extra_body(block.get("extra_body")),
        "output_format": (block.get("output_format") or "").strip(),
        "api_style": (block.get("api_style") or "").strip(),
    }


def _load_followup_model_from_config_file() -> dict[str, Any]:
    """Load follow-up API credentials from config.json.

    Preferred layout (Kimi 与 EAS 分开，互不影响)::

        "followup": {
          "active": "kimi",
          "kimi": { "enabled": true, "api_base": "...", "api_key": "...", "model": "..." },
          "eas": { "enabled": false, "api_base": ".../v1", "api_key": "EAS_TOKEN", "model": "...", "extra_body": {...} }
        }

    Legacy: a single ``followup_model`` object is treated as **Kimi only** (old demos).
    """
    try:
        root = Path(__file__).resolve().parent.parent
        cfg_path = root / "config.json"
        if not cfg_path.exists():
            return {}
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        fu = data.get("followup") or {}
        if fu:
            env_active = (os.getenv("FOLLOWUP_PROVIDER") or "").strip().lower()
            active = env_active or (fu.get("active") or fu.get("provider") or "kimi").strip().lower()
            if active not in ("kimi", "eas", "bailian"):
                active = "kimi"
            block = _merge_followup_block_from_env(active, fu.get(active) or {})
            out = _followup_block_from_config(block)
            if not out:
                return {}
            out["provider"] = active
            return out

        # Legacy: only Kimi-style single blob
        fm = data.get("followup_model", {}) or {}
        if not fm.get("enabled", True):
            return {}
        out = _followup_block_from_config(_merge_followup_block_from_env("kimi", fm))
        if not out:
            return {}
        out["provider"] = "kimi"
        return out
    except Exception:
        return {}


class FollowupQuestionClient:
    """Thin wrapper over an OpenAI-compatible chat completion endpoint."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        file_cfg = _load_followup_model_from_config_file()
        env_base = os.getenv("FOLLOWUP_API_BASE", "") or ""
        env_key = os.getenv("FOLLOWUP_API_KEY", "") or ""
        env_model = os.getenv("FOLLOWUP_MODEL", "") or ""
        env_extra_body = _parse_extra_body(os.getenv("FOLLOWUP_EXTRA_BODY"))

        # Precedence: constructor args > config.json > environment variables
        final_base = (api_base or file_cfg.get("api_base") or env_base or "").strip().rstrip("/")
        final_key = (api_key or file_cfg.get("api_key") or env_key or "").strip()
        final_model = (model or file_cfg.get("model") or env_model or "").strip()
        file_extra = file_cfg.get("extra_body") or {}
        merged_extra = {**env_extra_body, **file_extra}
        out_fmt = (
            (file_cfg.get("output_format") or "").strip()
            or (os.getenv("FOLLOWUP_OUTPUT_FORMAT") or "").strip()
            or "json"
        )
        api_style = (
            (file_cfg.get("api_style") or "").strip().lower()
            or (os.getenv("FOLLOWUP_API_STYLE") or "").strip().lower()
        )

        self._provider = (file_cfg.get("provider") or "").strip() or "unknown"
        self._api_base = final_base
        self._api_key = final_key
        self._model = final_model
        self._timeout = timeout
        self._extra_body = merged_extra
        self._default_output_format = out_fmt.lower()
        if not api_style:
            api_style = "responses" if self._provider == "bailian" else "chat_completions"
        if api_style not in ("chat_completions", "responses"):
            api_style = "chat_completions"
        self._api_style = api_style
        if self.enabled:
            LOG.info(
                "Follow-up model provider=%s style=%s base=%s model=%s",
                self._provider,
                self._api_style,
                self._api_base,
                self._model,
            )

    @property
    def enabled(self) -> bool:
        return bool(self._api_base and self._api_key and self._model)

    def describe_for_client(self) -> dict[str, Any]:
        """Safe subset for WebSocket JSON (no secrets)."""
        return {
            "provider": self._provider,
            "api_base": self._api_base,
            "model": self._model,
            "configured": self.enabled,
            "output_format": self._default_output_format,
            "api_style": self._api_style,
            "has_extra_body": bool(self._extra_body),
        }

    def _build_plain_prompts(
        self,
        asr_text: str,
        stage: str | None,
        dialogue_history: str | None,
        stage_role_summary: str,
        stage_hint: str,
    ) -> tuple[str, str]:
        history_block = ""
        if dialogue_history:
            history_block = (
                "以下是最近几轮面试官与证明人之间的对话记录（按时间顺序）：\n"
                f"{dialogue_history}\n\n"
            )
        system_prompt = (
            "你是“芝麻背调”背景调查访谈员的追问助手。主访谈员（语音模型）已按流程提问，你只负责在必要时补一刀追问。\n"
            "服务对象是候选人的证明人（前上级/同事等）；追问应挖掘候选人在该公司的履历与表现，"
            "不要把话题偏到证明人自身规划或无关经历。\n"
            + stage_role_summary
            + "追问范围（必须遵守）：\n"
            "- 仅当「对话里已经提到某一话题，但证明人回答缺少具体信息、案例、数据或可核对事实」时，"
            "才输出一句追问，把该话题挖深。\n"
            "- 对于主访谈员尚未问到、对话记录中完全未涉及的访谈模块/维度，必须输出「无」，"
            "把提问权交给主语音访谈员自行按流程推进；不要抢问未开启的话题。\n"
            "- 不要为了“提问而提问”去覆盖流程里尚未轮到或未提及的部分。\n"
            "输出规则（必须严格遵守）：\n"
            "- 不要输出思考过程、不要解释、不要 JSON、不要 markdown、不要编号列表。\n"
            "- 若不需要追问：只输出一个字「无」（不要引号、不要换行后还有内容）。\n"
            "- 若需要追问：只输出一句追问问题，一句内结束。\n"
        )
        user_prompt = (
            f"{stage_hint}{history_block}"
            f"证明人刚才的回答是：{asr_text or '（无识别内容）'}\n"
            "请直接给出「无」或一句追问。"
        )
        return system_prompt, user_prompt

    async def generate_followup(
        self,
        asr_text: str,
        stage: str | None = None,
        dialogue_history: str | None = None,
        output_format: str | None = None,
    ) -> str:
        """Generate a single short follow-up question based on ASR text and recent dialogue."""
        if not self.enabled:
            raise RuntimeError("FollowupQuestionClient is not properly configured")

        fmt = (output_format or self._default_output_format or "json").strip().lower()
        if fmt not in ("json", "plain"):
            fmt = "json"

        # Quick heuristic: for pure 寒暄/应答类短句，直接认为“不需要追问”。
        asr_norm = (asr_text or "").strip()
        trivial_replies = {
            "你好",
            "您好",
            "哈喽",
            "你好。",
            "您好。",
            "哈喽。",
            "是的",
            "是的是的",
            "对的",
            "好的",
            "好",
            "嗯",
            "嗯嗯",
            "行",
            "可以",
            "可以的",
            "方便的",
            "可以，方便的。",
            "能的",
            "能的能的",
        }
        if not asr_norm:
            return ""
        asr_compact = asr_norm.replace("，", "").replace("。", "").replace("！", "")
        if asr_compact in trivial_replies:
            LOG.info("followup.skip_by_heuristic asr=%r", asr_text)
            return ""

        stage_hint = ""
        stage_role_summary = (
            "主访谈员的角色与完整话术规范如下（仅供你参考，不要照抄）：\n"
            f"{DEFAULT_SYSTEM_ROLE}\n\n"
        )
        if stage:
            stage_hint = f"当前访谈阶段标记：{stage}。\n"

        if fmt == "plain":
            system_prompt, user_prompt = self._build_plain_prompts(
                asr_text, stage, dialogue_history, stage_role_summary, stage_hint
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 128,
            }
            raw = await self._request_model(payload)
            return self._parse_plain_followup(raw, asr_text)

        # JSON 决策（e2e / semi 默认）：便于解析 need_followup。
        history_block = ""
        if dialogue_history:
            history_block = (
                "以下是最近几轮面试官与证明人之间的对话记录（按时间顺序）：\n"
                f"{dialogue_history}\n\n"
            )

        system_prompt = (
            "你是“芝麻背调”背景调查访谈员的追问助手，服务对象是候选人的证明人（前上级/同事等），"
            "主访谈员（语音模型）已经按照既定的背景调查流程在提问。\n"
            "你的任务是：仅在有必要时，补充一个【额外】的追问问题，帮助挖掘候选人在该公司的履历与工作表现，"
            "避免遗漏关键信息，但绝不能把话题偏到证明人自己的职业经历或个人规划上。\n"
            + stage_role_summary
            + history_block
            + "追问范围（必须遵守）：\n"
            "- 仅当「对话里已经提到某一话题，但证明人回答缺少具体信息、案例、数据或可核对事实」时，"
            "才设置 need_followup=true 并给出一句追问。\n"
            "- 对于主访谈员尚未问到、对话记录中完全未涉及的模块/维度，必须 need_followup=false 且 question=\"\"，"
            "把后续提问交给主语音访谈员；不要抢问未开启的话题。\n"
            "- 不要为了“提问而提问”去覆盖流程里尚未轮到或未提及的部分。\n"
            "你必须先判断是否需要追问，然后以 JSON 格式给出决策。\n"
            "输出必须是**唯一一行**合法 JSON，形如：\n"
            '{"need_followup": false, "question": ""}\n'
            "字段含义：\n"
            '- need_followup：bool，是否需要现在就补充一个追问问题\n'
            '- question：string，当 need_followup 为 true 时，写一个简短的追问问题；为 false 时必须是空字符串\n'
            "严格约束：\n"
            "- 如果【没有明显缺口或主面试官后续极可能自然问到】，请返回 need_followup=false 且 question=\"\"。\n"
            "- 只有在你认为“现在就必须补充一个追问，否则信息会缺失”时，才设置 need_followup=true 并给出一个追问问题。\n"
            "- 不要复述候选人原话，不要输出解释分析，不要输出多条问题，不要加任何前缀。\n"
        )

        user_prompt = (
            f"{stage_hint}"
            f"候选人刚才的回答是：{asr_text or '（无识别内容）'}\n\n"
            "请先在心里判断是否存在需要【现在就补充】的追问点，然后用前面约定的 JSON 结构返回。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 64,
        }

        raw = await self._request_model(payload)

        # 尝试解析 JSON 决策；如果模型偶尔输出包裹在 ```json 里的内容，做一次简单清洗。
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if "\n" in cleaned:
                cleaned = cleaned.split("\n", 1)[-1]
        try:
            decision = json.loads(cleaned)
        except Exception:
            # 回退：如果解析失败，当成“需要追问”的一个自然语言问题。
            LOG.warning("followup.json_parse_failed raw=%r", raw)
            question_fallback = cleaned
            question_fallback = (question_fallback or "").strip()
            if not question_fallback:
                LOG.info("followup.no_question_after_parse_fail asr=%r", asr_text)
                return ""
            LOG.info("followup.use_fallback_question asr=%r question=%r", asr_text, question_fallback)
            return question_fallback

        need_followup = bool(decision.get("need_followup"))
        question = (decision.get("question") or "").strip()

        LOG.info(
            "followup.decision asr=%r need_followup=%s question=%r",
            asr_text,
            need_followup,
            question,
        )

        if not need_followup or not question:
            return ""

        return question

    async def _request_model(self, payload: dict[str, Any]) -> str:
        if self._api_style == "responses":
            return await self._responses(payload)
        return await self._chat_completion(payload)

    async def _chat_completion(self, payload: dict[str, Any]) -> str:
        body = {**payload, **self._extra_body}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # NOTE: _api_base is expected to be the full API base, e.g. "https://api.moonshot.cn/v1"
        async with httpx.AsyncClient(base_url=self._api_base, timeout=self._timeout) as client:
            resp = await client.post("/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        try:
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Unexpected response format from follow-up model: {data}") from exc
        return (text or "").strip()

    async def _responses(self, payload: dict[str, Any]) -> str:
        # OpenAI-compatible Responses API: https://.../compatible-mode/v1/responses
        request_payload: dict[str, Any] = {
            "model": payload.get("model"),
            "input": payload.get("messages") or [],
        }
        if payload.get("temperature") is not None:
            request_payload["temperature"] = payload.get("temperature")
        body = {**request_payload, **self._extra_body}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(base_url=self._api_base, timeout=self._timeout) as client:
            resp = await client.post("/responses", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        raise RuntimeError(f"Unexpected response format from follow-up model: {data}")

    def _parse_plain_followup(self, raw: str, asr_text: str) -> str:
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if "\n" in cleaned:
                cleaned = cleaned.split("\n", 1)[-1]
        first_line = cleaned.splitlines()[0].strip() if cleaned else ""
        if not first_line:
            return ""
        no_follow = {"无", "无。", "否", "不需要", "不用", "无追问"}
        if first_line in no_follow or first_line.startswith("无追问"):
            LOG.info("followup.plain_skip asr=%r first=%r", asr_text, first_line)
            return ""
        # 单行误输出「无」以外的否定
        if first_line in ("没有",):
            return ""
        LOG.info("followup.plain_question asr=%r question=%r", asr_text, first_line)
        return first_line

