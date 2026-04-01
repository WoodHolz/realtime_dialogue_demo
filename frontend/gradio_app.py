# -*- coding: utf-8 -*-
"""
Gradio UI for AI Interviewer Demo: mode switch, stage, prompt, link to real-time voice.
"""
from __future__ import annotations

import json
import gradio as gr
from config.prompt_templates import DEFAULT_SYSTEM_ROLE, DEFAULT_SPEAKING_STYLE, STAGE_PROMPTS

# Base URL for backend (same origin when served together)
# 使用相对路径向上一层，兼容 /voice 与 /s2s/voice 这种挂载方式。
BASE = ".."  # relative to current path


def build_voice_url(mode: str, stage: str) -> str:
    return f"{BASE}/voice?mode={mode}&stage={stage}"


def ui_blocks():
    with gr.Blocks(title="AI Interviewer Demo", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# AI Interviewer Realtime Voice Demo")
        gr.Markdown("使用豆包端到端实时语音模型，体验技术面试官。")

        with gr.Row():
            mode = gr.Radio(
                choices=["e2e", "semi"],
                value="e2e",
                label="Mode",
                info="End-to-End: 语音直接进模型。Semi: 识别后注入追问 prompt 再进模型。",
            )
            stage = gr.Dropdown(
                choices=list(STAGE_PROMPTS.keys()),
                value="introduction",
                label="Stage",
                info="动态阶段：开场 / 项目讨论 / 深度技术 / 压力面试",
            )

        with gr.Accordion("System Prompt (O 版本)", open=True):
            system_role = gr.Textbox(
                label="System Role",
                value=DEFAULT_SYSTEM_ROLE,
                lines=6,
                placeholder="系统人设与规则",
            )
            speaking_style = gr.Textbox(
                label="Speaking Style",
                value=DEFAULT_SPEAKING_STYLE,
                lines=2,
                placeholder="说话风格",
            )

        with gr.Row():
            start_btn = gr.Button("Start Interview (Open Real-Time Voice)", variant="primary")
            open_voice = gr.HTML("")

        def on_start(mode_val, stage_val):
            url = build_voice_url(mode_val, stage_val)
            return f'<a href="{url}" target="_blank" rel="noopener">Open Real-Time Voice Interview →</a> (opens in new tab)'

        start_btn.click(
            fn=on_start,
            inputs=[mode, stage],
            outputs=[open_voice],
        )

        gr.Markdown("""
        ### 说明
        - **End-to-End**: 麦克风 → 实时语音 API → 模型回复，测试 system prompt 长度与可控性、追问、延迟。
        - **Semi**: 用户语音 → API ASR → 后端注入追问 prompt → ChatTextQuery → 模型回复，测试 ASR 后可控性。
        - **Barge-in**: 用户说话时服务端会返回 ASRInfo，前端可停止播放并开始处理用户输入。
        - 请先设置环境变量 `DOUBAO_APP_ID` 与 `DOUBAO_ACCESS_KEY`。
        """)
    return demo


def ui_backcheck():
    """简洁版芝麻背调访谈展示页：使用最小化的 voice.html，在本页中对话。"""
    with gr.Blocks(title="芝麻背调访谈", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 芝麻背调证明人访谈")
        gr.Markdown(
            "以专业、亲和、严谨的方式，与证明人进行 5–7 分钟电话/视频访谈，"
            "核实候选人在上一家公司的履历与工作表现。"
        )

        # 在 Gradio 页面里嵌入最小化 voice.html（minimal=1：隐藏高级控制，仅保留开始/停止+文本）
        url = build_voice_url("e2e", "introduction") + "&minimal=1"
        gr.HTML(
            f"""
<div style="margin-top: 12px;">
  <iframe src="{url}" style="width: 100%; height: 720px; border: 1px solid #e5e7eb; border-radius: 12px;" allow="microphone; autoplay"></iframe>
</div>
            """
        )

        gr.Markdown(
            "#### 访谈流程概览\n"
            "- 开场与身份确认：说明来意，征得证明人同意\n"
            "- 基础信息核实：入离职时间、职位、职责、汇报关系等\n"
            "- 工作表现深度访谈：优点亮点、能力、态度、人际、管理等\n"
            "- 离职原因与合规性：排查违规违纪、竞业、劳动争议等风险\n"
            "- 综合评价与结束语：打分、建议、再次雇佣意愿等"
        )

    return demo


def ui_t2s_backcheck():
    """T2S 版芝麻背调访谈展示页：纯文本输入，支持直接输入或语音经豆包 ASR 识别后发送。"""
    with gr.Blocks(title="芝麻背调访谈 T2S", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 芝麻背调证明人访谈（T2S 纯文本 → 语音）")
        gr.Markdown(
            "支持直接以文本形式发起对话，或使用麦克风说话（豆包 ASR 识别后发送给语音对话模型）。"
            "服务端使用 `input_mod = text`，自动保证流式链路完整性。"
        )

        # 嵌入 t2sbg 纯文本输入页
        url = f"{BASE}/t2sbg-voice"
        gr.HTML(
            f"""
<div style="margin-top: 12px;">
  <iframe src="{url}" style="width: 100%; height: 720px; border: 1px solid #e5e7eb; border-radius: 12px;" allow="microphone; autoplay"></iframe>
</div>
            """
        )

        gr.Markdown(
            "#### 访谈流程概览\n"
            "- 开场与身份确认：说明来意，征得证明人同意\n"
            "- 基础信息核实：入离职时间、职位、职责、汇报关系等\n"
            "- 工作表现深度访谈：优点亮点、能力、态度、人际、管理等\n"
            "- 离职原因与合规性：排查违规违纪、竞业、劳动争议等风险\n"
            "- 综合评价与结束语：打分、建议、再次雇佣意愿等"
        )

    return demo


def main():
    # 默认本地启动时，用技术面试控制台
    demo = ui_blocks()
    demo.launch(server_name="0.0.0.0", server_port=7860)


if __name__ == "__main__":
    main()
