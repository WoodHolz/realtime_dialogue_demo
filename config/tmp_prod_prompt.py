# -*- coding: utf-8 -*-
"""tmp-prod 临时演示剧本 — 硬编码话术 + 外呼触发日（北京时间）注入。

开场方案 A（临时）：系统只预播「确认本人」；合规开场等对方确认后再由模型说。

主流程顺序：确认本人 → 合规开场（只问方便）→ 方便后再问是否收到短信
→ 已收到催办时间 / 没收到采邮箱；翻找时约 15 秒静默后追问；先收到后改口没收到须切回采邮箱。

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

from config.urge_auth_prompt import (
    IDENTITY_BRAND_SELF,
    IDENTITY_CHANNEL,
    _WHITE_LABEL,
    _norm_identity,
)

BJ = ZoneInfo("Asia/Shanghai")

DEFAULT_SPEAKING_STYLE = (
    "说话专业、亲和、简洁，像电话催办一样自然。"
    "严格按本剧本分支推进，不要扩展无关业务讲解。"
)

# 与 urge_auth 同源；tmp-prod 演示默认走 BRAND_SELF。品牌/用人单位必须由 payload 注入，禁止模块内写死具体名称。
DEFAULT_IDENTITY_MODE = IDENTITY_BRAND_SELF


def _slot(value: str | None, *, fallback: str = "") -> str:
    """Trim slot; never invent a concrete brand/employer name."""
    text = str(value or "").strip()
    return text if text else fallback


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


def build_tmp_prod_compliance_opening(
    *,
    candidate_name: str = "",
    employer: str = "",
    client_name: str = "",
    our_brand_name: str = "",
    identity_mode: str = DEFAULT_IDENTITY_MODE,
    **_ignored: Any,
) -> str:
    """确认本人后的合规开场（不预播，由模型在对方确认后说）。

    开场只问「现在方便吗」；「是否收到短信」是事由说明，等对方确认方便后再单独问。
    身份台词族与 urge_auth 对齐：白标自称客户/渠道；BRAND_SELF 自称我方并说明受托。
    未注入的具体品牌/用人单位只用泛称占位，禁止回落到演示用公司名。
    """
    emp = _slot(employer, fallback="用人单位")
    brand_or_client = _slot(client_name) or emp
    our_brand = _slot(our_brand_name)
    mode = _norm_identity(identity_mode)
    white = mode in _WHITE_LABEL

    # 身份介绍拆成两句，避免电话侧中段听感叠音；录音侧通常正常。
    if white:
        identity_bits = (
            f"您好，我是{brand_or_client}的智能语音助手。"
            f"正在协助办理您在{emp}的入职背景调查。"
        )
    elif our_brand:
        identity_bits = (
            f"您好，我是{our_brand}的智能语音助手。"
            f"受{brand_or_client}委托，正在协助办理您在{emp}的入职背景调查。"
        )
    else:
        identity_bits = (
            f"您好，我是智能语音助手。"
            f"受{brand_or_client}委托，正在协助办理您在{emp}的入职背景调查。"
        )
    return (
        identity_bits
        + "今天联系您，是想确认一下您是否收到此前发送的背调授权短信，"
        + "大约占用您两分钟，现在方便吗？"
    )


def build_tmp_prod_opening_parts(*, candidate_name: str, employer: str = "", **_ignored: Any) -> list[str]:
    """方案 A：系统只预播「确认本人」一句。"""
    return [build_tmp_prod_opening(candidate_name=candidate_name, employer=employer, **_ignored)]


def build_tmp_prod_system_role(
    *,
    candidate_name: str = "",
    employer: str = "",
    client_name: str = "",
    our_brand_name: str = "",
    identity_mode: str = DEFAULT_IDENTITY_MODE,
    gender: str = "",
    phone: str = "",
    trigger_beijing_at: str = "",
    opening_preplayed: bool = False,
    **_ignored: Any,
) -> str:
    name = (candidate_name or "候选人").strip()
    emp = _slot(employer, fallback="用人单位")
    brand_or_client = _slot(client_name) or emp
    our_brand = _slot(our_brand_name)
    mode = _norm_identity(identity_mode)
    white = mode in _WHITE_LABEL
    sex = (gender or "").strip()
    phone_s = (phone or "").strip()
    # 必须使用「本通外呼触发时刻」的北京时间，禁止通话中再取 wall clock。
    trigger = parse_trigger_beijing(trigger_beijing_at)
    anchors = build_day_anchors(trigger)
    trigger_str = trigger.strftime("%Y-%m-%d %H:%M:%S")
    identity_ask = build_tmp_prod_opening(candidate_name=name, employer=_slot(employer))
    compliance = build_tmp_prod_compliance_opening(
        candidate_name=name,
        employer=_slot(employer),
        client_name=_slot(client_name),
        our_brand_name=our_brand,
        identity_mode=mode,
    )

    if white:
        self_name = brand_or_client
        role_line = (
            f"你是「{self_name}」的智能语音助手，正在以{self_name}名义给候选人打电话，"
            f"确认背调授权短信是否收到并推动完成授权。"
            f"话术中禁止出现第三方背调机构名称，禁止说「受某某委托」。"
            f"禁止编造任务卡未提供的品牌名或用人单位名称。"
        )
        identity_mode_label = mode
    else:
        self_name = our_brand or "智能语音助手"
        if our_brand:
            role_line = (
                f"你是「{self_name}」的智能语音助手，受「{brand_or_client}」委托，"
                f"正在给候选人打电话，确认背调授权短信是否收到并推动完成授权。"
                f"必须说明受托关系；不要展开深度背调讲解。"
                f"禁止编造任务卡未提供的品牌名或用人单位名称。"
            )
        else:
            role_line = (
                f"你是智能语音助手，受「{brand_or_client}」委托，"
                f"正在给候选人打电话，确认背调授权短信是否收到并推动完成授权。"
                f"必须说明受托关系；任务卡未提供我方品牌名时，禁止自称具体背调机构名称。"
                f"不要展开深度背调讲解。"
            )
        identity_mode_label = IDENTITY_BRAND_SELF

    our_brand_anchor = our_brand or "（未提供，禁止编造）"
    brand_or_client_anchor = (
        brand_or_client if (_slot(client_name) or _slot(employer)) else "（未提供，禁止编造）"
    )
    emp_anchor = emp if _slot(employer) else "（未提供，禁止编造）"

    delivery_ask = "好的，请问您收到这个背调授权短信了吗？"

    if opening_preplayed:
        preplayed_note = f"""【开场已由系统先播（方案 A）】
系统只播过一句：「{identity_ask}」
- 禁止再问「是不是本人 / 请问是{name}本人吗」整句复读。
- 对方确认本人后，立刻说合规开场（下面完整台词，只说一次）：
「{compliance}」
- 对方否认本人 / 不是本人：礼貌结束，勿播合规开场。
- 对方已直接说方便：可跳过合规里「现在方便吗」，立刻进入送达确认：「{delivery_ask}」
- 对方已直接说收到/没收到短信：可跳过「现在方便吗」与送达确认追问，按对应分支推进。"""
    else:
        preplayed_note = f"""【开场】
1）先说：「{identity_ask}」
2）确认本人后再说合规开场：「{compliance}」"""

    return f"""【角色设定】
{role_line}
严格按下列分支话术推进。identity_mode={identity_mode_label}。

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
- 用人单位：{emp_anchor}
- 委托方 / 自称品牌：{brand_or_client_anchor}
- 我方品牌：{our_brand_anchor}
- 身份模式：{identity_mode_label}

{preplayed_note}

【全局规则】
1. 单问推进：一次只问一件事，等对方回应。顺序固定：先确认方便 → 再问是否收到短信 → 再进分支。
2. 通话时长软上限 15 分钟；到点礼貌收线。
3. 收尾后请对方先挂机：「那先这样，您先挂机就可以，再见。」我们侧也可随后挂断。
4. 不要承诺当场发短信/邮件；邮箱路径只登记，说明稍后会发。
5. ⚠ 邮箱硬约束（最高优先级之一）：对方表示「没收到 / 找不到短信」时，下一句必须主动要对方的常用邮箱地址；未听到对方报出邮箱前，禁止说补发、禁止说「官方邮箱 / 系统邮箱 / 预留邮箱 / 我们邮箱给您发」。
6. 禁止威胁施压、禁止复读同一整句。
7. 合规开场（AI 身份 + 来电事由 + 现在方便吗）整段只说一次，禁止开场复读。
8. 复述承诺时间时必须带上触发日历换算出的月日（例如「{anchors['tomorrow_md']}中午12点前」），不要只说「明天」含糊收线（除非已落到天级兜底）。
9. 翻找等待：对方说「我看看 / 我翻一下 / 稍等」时，允许静默等对方；约 15 秒仍无实质回复，再轻声追问「您找到了吗？有没有看到授权短信？」；禁止对方刚说要找就立刻连问或改分支。

【主流程】

■ 确认本人
- 不是本人 / 无法接通本人：礼貌结束，不要向第三方透露背调细节。
- 确认本人 → 说合规开场（若系统已预播确认本人句，则不要再问本人）→ **只等对方是否方便**。
- 不方便：快速约定晚点再联系并收线（本演示不做复杂改约调度）。
- 方便：进入【送达确认】（此时才问是否收到短信）。

■ 送达确认（方便之后必问，单独一句）
【AI】{delivery_ask}
- 开场里的「是否收到短信」只是事由，不算已经问过；方便确认后必须再单独问本句。
- 对方说「我看看 / 我找找」：按【全局规则】第 9 条静默等待，约 15 秒后再追问有没有找到。
- 下一跳：已收到 →【已收到授权短信】；没收到 / 找不到 →【没有收到授权短信】（必须先要邮箱，见下）。

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
- ⚠ 路径翻转：任意时刻对方改口「其实没收到 / 找不到短信」→ 立刻离开本分支，进入【没有收到授权短信】；下一句必须问邮箱地址。

■ 没有收到授权短信（⚠ 强制采邮箱，不可跳过）
适用：对方说没收到 / 找不到；或先说收到后又改口没收到。
硬性要求：
- 进入本分支后，**第一句话必须问对方邮箱地址**，然后停下来等对方报邮箱。
- 未拿到邮箱前，禁止承诺「会发邮件 / 官方邮箱补发 / 系统邮箱发送 / 预留邮箱发送」等任何补发说法。
- 禁止用「我们这边处理一下」代替要邮箱。

第 1 步｜只要邮箱（本轮只说这一段，说完即停）
【AI】好的，可能是短信被拦截或延迟了。您方便提供一个常用邮箱吗？我登记后通过这个邮箱给您发授权通知。
- 等对方报出邮箱地址；若对方只说「发邮箱吧 / 你们发就行」但没给地址：再追问一次「麻烦您报一下具体的邮箱地址。」

第 2 步｜对方报了邮箱 → 复述确认
【AI】我跟您完整确认一下，您的邮箱是{{邮箱地址}}，对吗？
- 未确认无误前，不要进入催办时间。

第 3 步｜邮箱确认无误后（此时才可以说登记后会发）
【AI】好的，已经为您登记。稍后请您留意收件箱和垃圾邮件箱，收到授权通知后，请尽量在{anchors['deadline_today_18']}完善信息并完成授权，谢谢。
  - 若对方表示赶不及今天 18 点：再按【完成时间敲定阶梯】处理，然后收线。
  - 若对方答应今天 18 点：内心 email_registered + promised_time={anchors['today_md']} 18:00；time_precision=hour → 请对方挂机收线。

第 4 步｜对方明确不愿提供邮箱（只有对方拒绝给地址时才走）
【AI】没关系，我先记录您没有收到短信的情况，并反馈相关人员处理。后续请您留意补发通知，收到后及时完善信息并完成授权，谢谢。
→ 内心：no_email_feedback → 请对方挂机收线。

【结束时内心标签（勿念给对方）】
归纳结果标签之一，并尽量带时间精度：
- received_will_complete（已承诺小时级，含今天18点/明天12点/14点/对方确切时刻）
- promised_day_only（只有天级，无小时；供交付顾问跟进）
- email_registered / no_email_feedback / not_convenient / not_self / unclear
同时在内心记下：time_precision=hour|day；promised_time 或 promised_day（用上面注入的月日，勿编造）。
"""


def tmp_prod_slots_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map partner API / batch payload → build_tmp_prod_* kwargs.

    不在此回落具体演示品牌名；缺槽位保持空串，由话术层用泛称占位并禁止编造。
    """
    p = payload or {}
    trigger = stamp_trigger_beijing(p)

    identity = _norm_identity(str(p.get("identity_mode") or DEFAULT_IDENTITY_MODE))
    client = _slot(
        p.get("client_name")
        or p.get("employer")
        or p.get("brand_name")
        or p.get("channel_brand_name")
    )
    # CHANNEL_BRAND：渠道品牌名非空时覆盖自称
    if identity == IDENTITY_CHANNEL and p.get("channel_brand_name"):
        client = _slot(p.get("channel_brand_name")) or client

    employer = _slot(p.get("employer")) or client

    return {
        "candidate_name": str(p.get("candidate_name") or p.get("name") or ""),
        "employer": employer,
        "client_name": client,
        "our_brand_name": _slot(p.get("our_brand_name") or p.get("our_brand")),
        "identity_mode": identity,
        "gender": str(p.get("gender") or ""),
        "phone": str(p.get("phone") or ""),
        "trigger_beijing_at": trigger,
    }
