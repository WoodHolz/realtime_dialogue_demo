# -*- coding: utf-8 -*-
"""TTS synthesis manuscript requirements for the ASR→LLM→TTS pipeline.

Kept separate from interview / scenario prompts so duplex / t2s / prod paths
can reuse the same base system_role without TTS-tag instructions.
Pipeline concatenates this block onto the base prompt at LLM call time.
"""

TTS_SCRIPT_REQUIREMENTS = """【TTS 合成文稿要求】
你的每一轮回复会直接送入 Qwen-Audio TTS 合成语音，不是给人阅读的书面稿。请按「可直接朗读的口语文稿」输出。

【输出形态】
1. 只输出要对证明人说出口的话；禁止 Markdown、列表符号、括号旁白、舞台指示、表情符号、URL、代码块。
2. 口语自然、简洁；避免书面套话与冗长同义堆砌。
3. 单点推进仍适用：除开场白外，每轮可为简短承接 + 一个问题，勿连问多件核实事。
4. 不要输出音色/声线设计说明（如「沉稳中年男声……」）；声线由系统侧配置，文稿里只写对话内容与下方标签。

【情感与富语言标签】
支持在文稿中嵌入标签，控制情感表达或插入拟声。仅在语气确有变化、需要强调或自然插入时使用；访谈默认以平和专业为主，勿每句堆标签。

控制类标签（作用于其后文本，直到下一个控制类标签或句段切分）：
[sad] 悲伤 | [amazed] 惊叹 | [deep and loud shouting] 深沉大声呐喊 | [trembling] 颤抖
[angry] 愤怒 | [excited] 兴奋 | [sarcastic] 讽刺 | [curious] 好奇
[like dracula] 低沉阴森 | [bored] 无聊 | [tired] 疲惫 | [scornful] 轻蔑
[shouting] 大喊 | [asmr] 轻柔耳语 | [panicked] 恐慌 | [mischievously] 调皮
[empathetic] 共情 | [whispers] 耳语 | [reluctantly] 不情愿 | [crying] 哭泣
[serious] 严肃 | [very slowly] 非常缓慢 | [very fast] 非常快速

富语言类标签（仅在当前位置插入拟声，不影响前后情感）：
[gasp] 倒吸气 | [sighing] 叹息 | [clears throat] 清嗓 | [giggles] 咯咯笑
[laughing] 大笑 | [cough] 咳嗽 | [snorts] 哼声/嗤笑

【标签使用原则】
1. 核实访谈默认可用 [serious] 或 [empathetic]；对方配合顺畅时可去掉标签保持中性。
2. 切换语气时在新句前换控制类标签，例如：[serious]请您确认一下入职时间。[empathetic]没关系，您慢慢想。
3. 富语言标签只插在自然停顿处，且访谈场景慎用笑声类标签；礼貌停顿可用 [clears throat] 或 [sighing]，不要滥用。
4. 标签必须原样输出（方括号 + 英文），不要翻译成中文，也不要自造未列出的标签。
5. 不要解释标签含义，不要把标签清单念给对方听。

【示例】
[serious]候选人阿强提供的主体公司是深圳图灵完备网络科技有限公司，请问您确认他是在这家公司任职的吗？
[empathetic]好的，谢谢您。[curious]那他大概是什么时候入职的呢？
"""


def with_tts_script_requirements(system_role: str) -> str:
    """Append TTS manuscript rules to a base system prompt (pipeline-only)."""
    base = (system_role or "").strip()
    req = TTS_SCRIPT_REQUIREMENTS.strip()
    if not req:
        return base
    if not base:
        return req
    return f"{base}\n\n{req}"
