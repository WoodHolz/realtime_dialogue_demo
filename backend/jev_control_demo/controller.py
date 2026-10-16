"""Closed-set Jev routing plus deterministic stage transitions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import time
from typing import Any

from .sop_prompts import render_sop_query


NO_GLOBAL = "NONE"
GLOBAL_THRESHOLD = 0.8
DEFAULT_MODEL = "jev-latest"

STAGE_BRANCHES: dict[str, dict[str, str]] = {
    "OPENING": {
        "7-2": "接听者明确表示不是本人、打错电话或无法找到本人。",
        "7-4": "电话由语音助手、智能助理或自动代接服务接听。",
        "7-5": "由家人、同事等其他真人代接，本人当前不在。",
        "7-6": "用户确认是本人并且现在方便继续沟通。",
        "7-8": "用户在开场直接表示授权已经填写或完成。",
        "OUT_OF_SCOPE": "无法确认是否本人或当前是否方便，需要只问一个澄清问题。",
    },
    "DELIVERY": {
        "7-36": "用户明确收到通知但尚未填写。",
        "7-37": "用户明确声称已经填写或完成授权。",
        "7-38": "用户明确没有收到通知。",
        "7-39": "用户不确定是否收到，需要时间查找。",
        "OUT_OF_SCOPE": "无法判断通知送达或完成状态，需要澄清。",
    },
    "UNFINISHED": {
        "7-42": "没填写是因为忙碌、忘记或拖延，没有技术故障。",
        "7-45": "用户确认有明确步骤遗漏，需要补完。",
        "7-47": "可能只差最终提交，需要确认页面提交状态。",
        "7-51": "明确卡在人脸识别或活体核验。",
        "7-53": "明确报告页面、链接、加载或报错故障。",
        "7-37": "用户说明现在已经全部完成，需要后台核验。",
        "OUT_OF_SCOPE": "未完成原因不清楚，需要只问一个澄清问题。",
    },
    "CHANNEL_SMS": {
        "7-40": "用户确认已经在短信渠道找到正确通知。",
        "7-41": "用户确认短信及拦截记录都没有找到。",
        "7-39": "用户仍在短信中查找或需要更多时间。",
        "OUT_OF_SCOPE": "短信查找结果不清楚，需要澄清。",
    },
    "CHANNEL_EMAIL": {
        "7-40": "用户确认已经在邮箱中找到正确通知。",
        "7-56": "用户愿意提供或确认邮箱以便处理补发。",
        "7-55": "邮箱也不可用、没有邮箱或拒绝提供，需要人工处理。",
        "7-39": "用户仍在邮箱中查找或需要更多时间。",
        "OUT_OF_SCOPE": "邮箱查找结果不清楚，需要澄清。",
    },
    "BACKEND_CHECK": {
        "7-46": "用户坚持已经完成，当前只需解释后台未同步并查询。",
        "7-47": "用户不确定是否最终提交，需要查看成功状态。",
        "7-45": "用户确认确实漏了步骤，需要继续补完。",
        "OUT_OF_SCOPE": "后台与用户说法不一致，需要澄清当前页面状态。",
    },
    "COMMITMENT": {
        "7-48": "用户接受建议时限并给出可执行完成时间。",
        "7-49": "用户拒绝、犹豫或无法接受当前时限。",
        "7-50": "用户给出明显过远的完成时间，需要协商收窄。",
        "OUT_OF_SCOPE": "尚未得到明确完成时间，需要澄清。",
    },
    "DONE": {"OUT_OF_SCOPE": "主线已经结束，仅澄清用户新的明确诉求。"},
}

GLOBAL_BRANCHES = {
    "7-9": "用户要求核验来电方身份或官方凭据。",
    "7-11": "用户怀疑诈骗、钓鱼、验证码或资金风险。",
    "7-14": "用户抱怨频繁来电或被打扰。",
    "7-18": "用户正在开车，需要立即安全收线。",
    "7-19": "用户有突发急事，需要立即停止沟通。",
    "7-20": "用户当前忙碌、不方便接听或要求稍后联系。",
    "7-23": "用户明确要求改用短信或邮件沟通。",
    "7-28": "用户明确拒绝继续沟通或要求停止联系。",
    "7-29": "用户担心在职状态被当前雇主知晓。",
    "7-31": "用户质疑隐私、数据使用或授权范围。",
    "7-32": "用户明确表示已经放弃offer或不再入职。",
    "7-34": "用户明确投诉或表达升级投诉诉求。",
    "7-35": "用户明确要求真人客服或人工处理。",
}

NEXT_STAGE = {
    ("OPENING", "7-2"): "DONE",
    ("OPENING", "7-4"): "DONE",
    ("OPENING", "7-5"): "DONE",
    ("OPENING", "7-6"): "DELIVERY",
    ("OPENING", "7-8"): "BACKEND_CHECK",
    ("DELIVERY", "7-36"): "UNFINISHED",
    ("DELIVERY", "7-37"): "BACKEND_CHECK",
    ("DELIVERY", "7-38"): "CHANNEL_SMS",
    ("UNFINISHED", "7-42"): "COMMITMENT",
    ("UNFINISHED", "7-37"): "BACKEND_CHECK",
    ("CHANNEL_SMS", "7-40"): "UNFINISHED",
    ("CHANNEL_SMS", "7-41"): "CHANNEL_EMAIL",
    ("CHANNEL_EMAIL", "7-40"): "UNFINISHED",
    ("CHANNEL_EMAIL", "7-55"): "DONE",
    ("BACKEND_CHECK", "7-45"): "UNFINISHED",
    ("BACKEND_CHECK", "7-47"): "UNFINISHED",
    ("COMMITMENT", "7-48"): "DONE",
}
TERMINAL_GLOBALS = {"7-18", "7-19", "7-28", "7-32", "7-34"}


@dataclass(frozen=True)
class JevSopDecision:
    sop_id: str
    stage_before: str
    stage_after: str
    is_global: bool
    confidence: float
    latency_ms: float
    query: str


class JevSopController:
    def __init__(self, *, model: str | None = None, initial_stage: str = "OPENING", async_progress: bool = False) -> None:
        if initial_stage not in STAGE_BRANCHES:
            raise ValueError(f"unknown initial stage: {initial_stage}")
        self.model = model or os.environ.get("JEV_MODEL", DEFAULT_MODEL)
        self.stage = initial_stage
        self._route_lock = asyncio.Lock()
        self.async_progress = async_progress
        self.turn_id = 0
        self.status: dict[str, Any] = {}
        self.active_decision: JevSopDecision | None = None
        self.user_text = ""

    async def apply_progress(self, turn_id: int, patch: dict, assistant_text: str) -> bool:
        async with self._route_lock:
            if turn_id != self.turn_id or self.active_decision is None:
                return False
            evidence = patch.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                return False
            if evidence not in self.user_text and evidence not in assistant_text:
                return False
            facts = patch.get("facts", {})
            allowed = {"identity_confirmed", "available_to_talk", "notification_received", "authorization_claimed_complete"}
            # Only user-grounded evidence may mutate user facts.
            if isinstance(facts, dict) and evidence in self.user_text:
                self.status.update({k: v for k, v in facts.items() if k in allowed and type(v) is bool})
            self.status["pending_question"] = str(patch.get("pending_question", ""))[:500]
            self.status["sop_completed"] = patch.get("completed") is True
            if patch.get("completed") is True:
                d = self.active_decision
                if not d.is_global:
                    self.stage = NEXT_STAGE.get((d.stage_before, d.sop_id), d.stage_before)
            return True

    async def route(self, user_text: str, history: list[str] | None = None) -> JevSopDecision:
        async with self._route_lock:
            result = await asyncio.to_thread(self._route_sync, user_text, list(history or []))
            self.turn_id += 1
            self.active_decision = result
            self.user_text = user_text
            return result

    def _route_sync(self, user_text: str, history: list[str]) -> JevSopDecision:
        try:
            from typesafe_sdk import Choice, TypeSafeClient
        except ImportError as exc:
            raise RuntimeError("Jev demo requires typesafe-sdk; install project requirements") from exc

        stage_before = self.stage
        state = {
            "current_stage": stage_before,
            "recent_dialogue": history[-6:],
            "user_message": user_text,
            "status": dict(self.status),
            "last_sop_id": self.active_decision.sop_id if self.active_decision else None,
        }
        started = time.perf_counter()
        with TypeSafeClient(model=self.model) as client:
            response = client.system_one(
                state=state,
                questions={
                    "stage_branch": Choice(
                        instructions="Which current-stage branch best matches the latest user_message?",
                        criteria=STAGE_BRANCHES[stage_before],
                    ),
                    "global_side_branch": Choice(
                        instructions="Does the latest user_message require a global side branch?",
                        criteria={
                            NO_GLOBAL: "No global interruption is present; continue the current stage.",
                            **GLOBAL_BRANCHES,
                        },
                    ),
                },
                model=self.model,
            )
        latency_ms = (time.perf_counter() - started) * 1000
        local = response.choices["stage_branch"]
        side = response.choices["global_side_branch"]
        side_probability = float(side.probabilities[side.choice])
        is_global = (
            side.choice != NO_GLOBAL
            and float(side.confidence) >= GLOBAL_THRESHOLD
            and side_probability >= GLOBAL_THRESHOLD
        )
        selected = side if is_global else local
        sop_id = selected.choice
        if is_global:
            stage_after = "DONE" if sop_id in TERMINAL_GLOBALS else stage_before
        else:
            stage_after = NEXT_STAGE.get((stage_before, sop_id), stage_before)
            if self.async_progress:
                stage_after = stage_before
        self.stage = stage_after
        guidance = render_sop_query(sop_id, stage=stage_before)
        query = (
            "【用户刚才的原话】\n"
            f"{user_text}\n\n"
            "【内部对话指导】\n"
            f"{guidance}\n\n"
            "请直接生成下一句对用户说的话，不要输出分析过程。"
        )
        return JevSopDecision(
            sop_id=sop_id,
            stage_before=stage_before,
            stage_after=stage_after,
            is_global=is_global,
            confidence=float(selected.confidence),
            latency_ms=round(latency_ms, 2),
            query=query,
        )
