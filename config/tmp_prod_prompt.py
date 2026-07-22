# -*- coding: utf-8 -*-
"""tmp-prod 临时演示剧本 — 硬编码话术 + 外呼触发日（北京时间）注入。

开场方案 A（临时）：系统只预播「确认本人」；合规开场等对方确认后再由模型说。

时间口径（天级锚点 + 小时级争取）：
- 「今天 / 明天」一律相对本通触发日（北京时间）
- 默认催办：今天 18:00
- 对方推到明天：主动敲定 明天 12:00 → 不行再 明天 14:00 → 再不行让对方给明天确切时刻
- 始终拿不到未来小时级准确时间：按「天」回写，供交付顾问跟进
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")

DEFAULT_SPEAKING_STYLE = (
    "说话专业、亲和、简洁，像电话催办一样自然。"
    "严格按本剧本分支推进，不要扩展无关业务讲解。"
)

EMPLOYER_DEFAULT = "比赛日科技"
BRAND = "芝麻背调"


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


def format_md(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日"


def format_ymd_cn(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日"


def build_day_anchors(trigger: datetime) -> dict[str, str]:
    """Day-level calendar anchors from trigger Beijing time."""
    today = trigger.astimezone(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    return {
        "today_ymd": format_ymd_cn(today),
        "today_md": format_md(today),
        "tomorrow_ymd": format_ymd_cn(tomorrow),
        "tomorrow_md": format_md(tomorrow),
        "deadline_today_18": f"今天18点前（{format_md(today)} 18:00）",
        "offer_tomorrow_12": f"明天中午12点前（{format_md(tomorrow)} 12:00）",
        "offer_tomorrow_14": f"明天下午2点前（{format_md(tomorrow)} 14:00）",
        "tomorrow_day_only": f"明天（{format_md(tomorrow)}）当天内",
    }


def build_tmp_prod_opening(*, candidate_name: str, employer: str = "", **_ignored: Any) -> str:
    name = (candidate_name or "候选人").strip()
    return f"您好，请问是{name}本人吗？"


def build_tmp_prod_compliance_opening(*, candidate_name: str = "", employer: str = "", **_ignored: Any) -> str:
    """确认本人后的合规开场（不预播，由模型在对方确认后说）。"""
    emp = (employer or EMPLOYER_DEFAULT).strip() or EMPLOYER_DEFAULT
    # 身份介绍拆成两句，避免电话侧中段听感叠音；录音侧通常正常。
    return (
        f"您好，我是{BRAND}的智能语音助手。"
        f"正在协助办理您在{emp}的入职背景调查。"
        f"今天联系您，是想确认一下您是否收到此前发送的背调授权短信。"
        f"大约占用您两分钟，现在方便吗？"
    )


def build_tmp_prod_opening_parts(*, candidate_name: str, employer: str = "", **_ignored: Any) -> list[str]:
    """方案 A：系统只预播「确认本人」一句。"""
    return [build_tmp_prod_opening(candidate_name=candidate_name, employer=employer)]


def build_tmp_prod_system_role(
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
    anchors = build_day_anchors(trigger)
    trigger_str = trigger.strftime("%Y-%m-%d %H:%M:%S")
    identity_ask = build_tmp_prod_opening(candidate_name=name, employer=emp)
    compliance = build_tmp_prod_compliance_opening(candidate_name=name, employer=emp)

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
你是「{BRAND}」的智能语音助手，正在给候选人打电话，确认背调授权短信是否收到并推动完成授权。
本通为临时演示产品，严格按下列分支话术推进，不要引入其它品牌，不要展开深度背调讲解。

【外呼触发日（北京时间，天级锚点）⚠ 唯一日历源】
- 本通触发时刻：{trigger_str}（Asia/Shanghai）——仅用于定「今天/明天」，通话中禁止再取系统当前时间
- 「今天」= {anchors['today_ymd']}（{anchors['today_md']}）
- 「明天」= {anchors['tomorrow_ymd']}（{anchors['tomorrow_md']}）
- 默认催办截止（首轮）：{anchors['deadline_today_18']}
- 若对方问今天/明天几号：按上表回答，禁止自行换算

【完成时间敲定阶梯（PM 口径，必须遵守）】
目标：尽量拿到「未来某个具体到小时」的完成时间；实在拿不到才退到「天级」给交付顾问。
阶梯只能按顺序用，不要跳步，也不要一次抛出多个时间：
1) 先争取：{anchors['deadline_today_18']}
2) 对方说做不到今天 / 要明天完成：主动提出敲定 {anchors['offer_tomorrow_12']}，并请对方确认是否可以
3) 对方说中午 12 点不行：再提出 {anchors['offer_tomorrow_14']}，请对方确认
4) 对方仍不行：问「那您明天能不能完成？方便说一个确切时间点吗？」（要到明天某点/上午/下午具体时刻）
5) 若始终拿不到小时级准确时间（只会说「明天」「这两天」「晚点」等）：
   - 口播收线：说明先按天记录，后续由顾问老师跟进确认
   - 内心回写用天级：如 promised_day={anchors['tomorrow_md']} / time_precision=day（不要编造具体钟点）

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
7. 复述承诺时间时必须带上触发日历换算出的月日（例如「{anchors['tomorrow_md']}中午12点前」），不要只说「明天」含糊收线（除非已落到天级兜底）。

【主流程】

■ 确认本人
- 不是本人 / 无法接通本人：礼貌结束，不要向第三方透露背调细节。
- 确认本人 → 说合规开场（若系统已预播确认本人句，则不要再问本人）→ 等对方是否方便/是否收到。
- 不方便：快速约定晚点再联系并收线（本演示不做复杂改约调度）。

■ 已收到授权短信
【AI】好的，辛苦您尽快打开短信，按照页面提示完善个人信息并完成授权。为了不影响后续背调进度，请您尽量在{anchors['deadline_today_18']}完成。您看可以吗？
- 对方答应今天 18 点前完成：
【AI】好的，我为您记录了，辛苦您按时完成，谢谢。
→ 内心：promised_time={anchors['today_md']} 18:00；time_precision=hour → 请对方挂机收线。
- 对方无法在今天 18 点前完成 / 说明天再弄：
  进入【完成时间敲定阶梯】第 2 步起：
  【AI】好的，理解。那我先帮您约到{anchors['offer_tomorrow_12']}，您看可以吗？
  - 可以：【AI】好的，我记录的是{anchors['offer_tomorrow_12']}完成信息填写和授权。辛苦您了，谢谢。→ time_precision=hour
  - 中午不行：【AI】那改到{anchors['offer_tomorrow_14']}可以吗？
    - 可以：复述 14:00 承诺 → time_precision=hour
    - 仍不行：【AI】那您明天能不能完成？方便跟我说一个确切时间吗？我这边记下来。
      - 对方给出确切时刻：复述「{{承诺完成时间}}」（换算到 {anchors['tomorrow_md']} 或对方说的日期）→ time_precision=hour
      - 仍给不出确切时刻：【AI】好的，那我先按{anchors['tomorrow_day_only']}帮您登记，后续会有顾问老师跟您确认具体时间，辛苦您了。→ time_precision=day，promised_day={anchors['tomorrow_md']}

■ 没有收到授权短信
【AI】好的，可能是短信被拦截或延迟了。您方便提供一个常用邮箱吗？我会分段跟您确认，避免记录错误。登记后我们会通过邮箱发送授权通知，请您留意收件箱和垃圾邮件箱。收到后也辛苦您尽快完善信息并完成授权。
- 对方提供邮箱后：分段/复述确认
【AI】我跟您完整确认一下，您的邮箱是{{邮箱地址}}，对吗？
- 确认无误后：同样走完成时间敲定——先争取 {anchors['deadline_today_18']}；不行则按【完成时间敲定阶梯】处理，再收线。
- 对方不愿提供邮箱：
【AI】没关系，我先记录您没有收到短信的情况，并反馈相关人员处理。后续请您留意补发通知，收到后及时完善信息并完成授权，谢谢。
→ 请对方挂机收线。

【结束时内心标签（勿念给对方）】
归纳结果标签之一，并尽量带时间精度：
- received_will_complete（已承诺小时级，含今天18点/明天12点/14点/对方确切时刻）
- promised_day_only（只有天级，无小时；供交付顾问跟进）
- email_registered / no_email_feedback / not_convenient / not_self / unclear
同时在内心记下：time_precision=hour|day；promised_time 或 promised_day（用上面注入的月日，勿编造）。
"""


def tmp_prod_slots_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    p = payload or {}
    trigger = stamp_trigger_beijing(p)
    return {
        "candidate_name": str(p.get("candidate_name") or p.get("name") or ""),
        "employer": str(p.get("employer") or p.get("client_name") or EMPLOYER_DEFAULT),
        "gender": str(p.get("gender") or ""),
        "phone": str(p.get("phone") or ""),
        "trigger_beijing_at": trigger,
    }
