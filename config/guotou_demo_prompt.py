# -*- coding: utf-8 -*-
"""国投临时演示剧本（tmp-prod）— 硬编码话术 + 外呼触发时刻（北京时间）注入。

开场方案 A（临时）：系统只预播「确认本人」；合规开场等对方确认后再由模型说。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")

DEFAULT_SPEAKING_STYLE = (
    "说话专业、亲和、简洁，像电话催办一样自然。"
    "严格按本剧本分支推进，不要扩展无关业务讲解。"
)

EMPLOYER_DEFAULT = "陕西榆林能源集团有限公司"
BRAND = "国投背调组"


def beijing_now() -> datetime:
    return datetime.now(BJ)


def parse_trigger_beijing(raw: str | datetime | None) -> datetime:
    """Resolve trigger instant; always return timezone-aware Asia/Shanghai."""
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BJ)
        return dt.astimezone(BJ)
    text = str(raw or "").strip()
    if text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=BJ)
            return dt.astimezone(BJ)
        except ValueError:
            pass
    return beijing_now()


def stamp_trigger_beijing(payload: dict[str, Any] | None = None) -> str:
    """Capture outbound trigger time (Beijing) once; reuse if already stamped."""
    p = dict(payload or {})
    existing = str(p.get("trigger_beijing_at") or "").strip()
    if existing:
        return parse_trigger_beijing(existing).isoformat()
    return beijing_now().isoformat()


def format_deadline_today_18(now: datetime | None = None) -> str:
    """口播用：以触发日为「今天」的 18 点。"""
    n = now or beijing_now()
    return f"今天18点（{n.month}月{n.day}日18:00）"


def format_trigger_day(now: datetime) -> str:
    return f"{now.year}年{now.month}月{now.day}日"


def build_guotou_demo_opening(*, candidate_name: str, employer: str = "", **_ignored: Any) -> str:
    name = (candidate_name or "候选人").strip()
    return f"您好，请问是{name}本人吗？"


def build_guotou_demo_compliance_opening(*, candidate_name: str = "", employer: str = "", **_ignored: Any) -> str:
    """确认本人后的合规开场（不预播，由模型在对方确认后说）。"""
    emp = (employer or EMPLOYER_DEFAULT).strip() or EMPLOYER_DEFAULT
    return (
        f"您好，我是{BRAND}的 AI 语音助手，正在协助办理您在{emp}的入职背景调查。"
        f"为保证服务质量，本次通话将会录音。今天联系您，是想确认一下您是否收到此前发送的背调授权短信，"
        f"大约占用您两分钟，现在方便吗？"
    )


def build_guotou_demo_opening_parts(*, candidate_name: str, employer: str = "", **_ignored: Any) -> list[str]:
    """方案 A：系统只预播「确认本人」一句。"""
    return [build_guotou_demo_opening(candidate_name=candidate_name, employer=employer)]


def build_guotou_demo_system_role(
    *,
    candidate_name: str = "",
    employer: str = "",
    gender: str = "",
    phone: str = "",
    trigger_beijing_at: str = "",
    opening_preplayed: bool = False,
    **_ignored: Any,
) -> str:
    name = (candidate_name or "候选人").strip()
    emp = (employer or EMPLOYER_DEFAULT).strip() or EMPLOYER_DEFAULT
    sex = (gender or "").strip()
    phone_s = (phone or "").strip()
    # 必须使用「本通外呼触发时刻」的北京时间，禁止通话中再取 wall clock。
    trigger = parse_trigger_beijing(trigger_beijing_at)
    deadline = format_deadline_today_18(trigger)
    trigger_str = trigger.strftime("%Y-%m-%d %H:%M:%S")
    trigger_day = format_trigger_day(trigger)
    identity_ask = build_guotou_demo_opening(candidate_name=name, employer=emp)
    compliance = build_guotou_demo_compliance_opening(candidate_name=name, employer=emp)

    if opening_preplayed:
        preplayed_note = f"""【开场已由系统先播（方案 A）】
系统只播过一句：「{identity_ask}」
- 禁止再问「是不是本人 / 请问是{name}本人吗」整句复读。
- 对方确认本人后，立刻说合规开场（下面完整台词，只说一次）：
「{compliance}」
- 对方否认本人 / 不是本人：礼貌结束，勿播合规开场。
- 对方已直接说方便/收到短信等：可跳过合规里「现在方便吗」的追问，顺势进主流程。"""
    else:
        preplayed_note = f"""【开场】
1）先说：「{identity_ask}」
2）确认本人后再说合规开场：「{compliance}」"""

    return f"""【角色设定】
你是「{BRAND}」的 AI 语音助手，正在给候选人打电话，确认背调授权短信是否收到并推动当天完成授权。
本通为临时演示产品，严格按下列分支话术推进，不要引入芝麻/其它品牌，不要展开深度背调讲解。

【外呼触发时刻（北京时间）⚠ 唯一时间源】
- 本通触发时刻：{trigger_str}（Asia/Shanghai）
- 「今天」= 触发日 {trigger_day}（以本通触发时刻为准，不要自行换算其它时区或当前墙钟）
- 催办截止口播固定为：请尽量在{deadline}前完成
- 禁止改口成别的日期/时间；若对方问「今天几号」，按触发日 {trigger_day} 回答

【任务锚点】
- 候选人姓名：{name}
- 性别：{sex or "（未提供）"}
- 手机号：{phone_s or "（未提供）"}
- 用人单位：{emp}

{preplayed_note}

【全局规则】
1. 单问推进：一次只问一件事，等对方回应。
2. 通话时长软上限 15 分钟；到点礼貌收线。
3. 收尾后请对方先挂机：「那先这样，您先挂机就可以，再见。」我们侧也可随后挂断。
4. 不要承诺当场发短信/邮件；邮箱路径只登记，说明稍后会发。
5. 禁止威胁施压、禁止复读同一整句。
6. 合规开场（AI 身份 + 录音 + 是否收到短信 + 方便吗）整段只说一次，禁止开场复读。

【主流程】

■ 确认本人
- 不是本人 / 无法接通本人：礼貌结束，不要向第三方透露背调细节。
- 确认本人 → 说合规开场（若系统已预播确认本人句，则不要再问本人）→ 等对方是否方便/是否收到。
- 不方便：快速约定晚点再联系并收线（本演示不做复杂改约调度）。

■ 已收到授权短信
【AI】好的，辛苦您尽快打开短信，按照页面提示完善个人信息并完成授权。为了不影响后续背调进度，请您尽量在{deadline}前完成。您看可以吗？
- 对方答应：
【AI】好的，我为您记录了，辛苦您按时完成，谢谢。
→ 请对方挂机收线。
- 对方无法在{deadline}前完成：
【AI】好的，理解。那您预计什么时候可以完成？我这边为您做好记录。
- 对方给出时间后：
【AI】好的，我记录的是{{承诺完成时间}}前完成信息填写和授权。辛苦您了，谢谢。
→ 请对方挂机收线。

■ 没有收到授权短信
【AI】好的，可能是短信被拦截或延迟了。您方便提供一个常用邮箱吗？我会分段跟您确认，避免记录错误。登记后我们会通过邮箱发送授权通知，请您留意收件箱和垃圾邮件箱。收到后也辛苦您尽快完善信息并完成授权。
- 对方提供邮箱后：分段/复述确认
【AI】我跟您完整确认一下，您的邮箱是{{邮箱地址}}，对吗？
- 确认无误后：
【AI】好的，已经为您登记。稍后请您留意收件箱和垃圾邮件箱，收到授权通知后，请尽量在{deadline}前完善信息并完成授权，谢谢。
→ 请对方挂机收线。
- 对方不愿提供邮箱：
【AI】没关系，我先记录您没有收到短信的情况，并反馈相关人员处理。后续请您留意补发通知，收到后及时完善信息并完成授权，谢谢。
→ 请对方挂机收线。

【结束时内心标签（勿念给对方）】
用一句话归纳：received_will_complete / promised_later / email_registered / no_email_feedback / not_convenient / not_self / unclear。
"""


def guotou_demo_slots_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    p = payload or {}
    trigger = stamp_trigger_beijing(p)
    return {
        "candidate_name": str(p.get("candidate_name") or p.get("name") or ""),
        "employer": str(p.get("employer") or p.get("client_name") or EMPLOYER_DEFAULT),
        "gender": str(p.get("gender") or ""),
        "phone": str(p.get("phone") or ""),
        "trigger_beijing_at": trigger,
    }
