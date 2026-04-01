# AI Interviewer Realtime Voice Demo

Web demo of an **AI technical interviewer** using the **Doubao (豆包) Realtime Voice API** (火山引擎端到端实时语音大模型). The app supports two modes and tests prompt controllability, dynamic prompt updates, follow-up questioning, and barge-in.

## Architecture

```
Browser (Gradio UI / Voice page)
     │ WebSocket (audio + control)
     ▼
Python Backend (FastAPI)
     │ WebSocket (binary protocol)
     ▼
Doubao Realtime Voice API
```

- **Frontend**: Gradio (mode, stage, system prompt) + standalone real-time voice page (WebSocket, mic, transcript, playback).
- **Backend**: Python async WebSocket server that bridges the browser to the Doubao API, with prompt manager and latency logging.

## Modes

1. **End-to-End (e2e)**  
   User speech → Realtime Voice API → model reply. Tests system prompt length/controllability, follow-up, and latency.

2. **Semi End-to-End (semi)**  
   User speech → API ASR → backend injects follow-up prompt → ChatTextQuery → model reply. Tests ASR interception and prompt injection.

## Features

- **System prompt**: Configurable `system_role` and `speaking_style` (O version).
- **Dynamic stage**: introduction → project_discussion → deep_technical → pressure (stage prompts in `config/prompt_templates.py`).
- **Barge-in**: On ASRInfo from the server, playback is stopped and user input is processed (handled in frontend and backend).
- **Latency**: Backend logs `user_speech_end`, `asr_complete`, `llm_response_start`, `tts_start`; `total_latency = tts_start - user_speech_end` is logged.

## Setup

1. **Environment**

   Create a `.env` or export:

   ```bash
   export DOUBAO_APP_ID="your-app-id"
   export DOUBAO_ACCESS_KEY="your-access-key"
   ```

   Get these from the [Volc Engine Console](https://www.volcengine.com/docs/6561/196768).

2. **Install** (recommend a virtualenv)

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. **Run**

   ```bash
   python run.py
   ```

Then open:

- **http://localhost:7998** — index with links
- **http://localhost:7998/gradio** — Gradio UI (mode, stage, prompt, link to voice)
- **http://localhost:7998/voice** — real-time voice interview (WebSocket)

   For voice with mode/stage from Gradio, use “Start Interview” on the Gradio page; it opens `/voice?mode=e2e&stage=introduction`.

## Project layout

```
ai_interviewer_demo/
  backend/
    server.py           # FastAPI + WebSocket bridge
    realtime_client.py  # Doubao API WebSocket client
    protocol.py         # Binary protocol encode/decode
    audio_stream.py     # PCM chunking (16k, 20ms)
    prompt_manager.py  # Stage + custom prompt
    latency_logger.py  # Timestamps per turn
  config/
    prompt_templates.py # System prompts + semi wrapper
  frontend/
    gradio_app.py      # Gradio UI
    index.html         # Entry links
    voice.html         # Real-time voice (mic + WS + playback)
  run.py
  requirements.txt
```

## API notes (Doubao)

- **Audio in**: PCM 16 kHz, mono, s16le; recommended 20 ms per chunk (640 bytes).
- **Audio out**: Configured as PCM 24 kHz s16le for easier browser playback (optional in `realtime_client.py`).
- **Barge-in**: Server sends ASRInfo when user starts speaking; client should stop TTS playback and handle new input.
- **Semi mode**: After ASREnded, backend wraps the final ASR text with `wrap_asr_for_semi()` and sends it via ChatTextQuery.

## Verification: 动态 Prompt 控制聊天内容

以 **eBPF 技术面试** 为例的验证流程（前置 prompt 长度/细粒度 + 过程中修改 prompt 的时机与方式），见：

- **[docs/VERIFICATION_DYNAMIC_PROMPT.md](docs/VERIFICATION_DYNAMIC_PROMPT.md)** — 阶段设计、建议 Prompt 文案、操作步骤与验证清单。

## Deliverable checklist

- [x] Web UI (Gradio): mode switch, prompt/stage, start interview link
- [x] Real-time voice page: mic input, transcript, AI playback
- [x] End-to-end and semi end-to-end modes
- [x] Dynamic prompt (stage + custom system_role/speaking_style)
- [x] Barge-in (stop playback on ASRInfo)
- [x] Backend latency logging (user_speech_end → tts_start)
