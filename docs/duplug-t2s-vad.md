# t2s + SoulX-Duplug VAD（外部服务）/ t2s + SoulX-Duplug VAD (external server)

## TL;DR
- **中文**：启动 SoulX-Duplug `ws://127.0.0.1:7997/turn` 服务，然后启动本项目后端并设置 `DUPLUG_VAD_ENABLED=true`。`t2s` 模式里后端会把 16k PCM 同时送给 ASR 与 Duplug VAD：`nonidle` 触发 barge-in；`speak` 后默认**等待 700ms**再结束 ASR（`config.json` → `debug.t2s_speak_grace_ms`，环境变量 `T2S_SPEAK_GRACE_MS` 可覆盖；`0` = 立即结束），等待期内若再次出现 `nonidle` 则取消结束并继续录制。首段语音 ASR pre-roll 默认 **1000ms**：`debug.t2s_pre_roll_ms`，`T2S_PRE_ROLL_MS` 可覆盖。
- **English**: Start SoulX-Duplug at `ws://127.0.0.1:7997/turn`, then run this backend with `DUPLUG_VAD_ENABLED=true`. In `t2s`, backend streams PCM to ASR + Duplug VAD (`nonidle` => barge-in; after `speak`, **700ms** grace by default — `config.json` `debug.t2s_speak_grace_ms`, overridable with `T2S_SPEAK_GRACE_MS`; `0` = immediate finalize; any `nonidle` during grace cancels). ASR pre-roll default **1000ms**: `debug.t2s_pre_roll_ms`, overridable with `T2S_PRE_ROLL_MS`.

## Data flow / 数据流（工程实现）
- **中文**：`t2sbg` 页面是 **Browser -> Backend -> (Duplug VAD + ASR)**。浏览器只把麦克风 PCM 送到项目后端；后端再把同一份音频分发到 VAD 和 ASR，并根据 VAD state（`nonidle/speak/idle`）控制 barge-in 与收尾。浏览器**不直连** Duplug VAD 服务。
- **English**: `t2sbg` uses **Browser -> Backend -> (Duplug VAD + ASR)**. Browser only uploads mic PCM to this backend; backend fans out the same audio to VAD + ASR and uses VAD states (`nonidle/speak/idle`) to drive barge-in and completion. Browser does **not** connect to Duplug directly.

## 0) 地址与访问范围 / Address and reachability
- 当前建议启动参数是 `--host 127.0.0.1 --port 7997`，因此只在**本机/同网络命名空间**可访问。
- If you need cross-machine access, use `--host 0.0.0.0` or put it behind a reverse proxy.

## 1) 启动 SoulX-Duplug 服务 / Run SoulX-Duplug server

参考仓库 / Reference: [Soul-AILab/SoulX-Duplug](https://github.com/Soul-AILab/SoulX-Duplug)

```bash
git clone https://github.com/Soul-AILab/SoulX-Duplug.git
cd SoulX-Duplug
```

### ArchLinux 依赖 / ArchLinux dependencies
```bash
yay -S ffmpeg sox
```

### Python 环境 / Python environment
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 模型下载 / Model download
```bash
huggingface-cli download --resume-download Soul-AILab/SoulX-Duplug-0.6B --local-dir pretrained_models
```

### 启动端口 7997 / Start on port 7997
```bash
uvicorn server:app --host 127.0.0.1 --port 7997 --workers 1
```

## 2) 启动本项目后端 / Run this backend
在 `realtime_voice_interviewer_demo` 根目录：

```bash
export DUPLUG_VAD_ENABLED=true
export DUPLUG_VAD_URL="ws://127.0.0.1:7997/turn"
python run.py
```

## 3) 前端使用 / Frontend usage
- 打开 `t2sbg` 页面（`/t2sbg-voice` 或 `/t2sbg`）。
- 建议关闭“本地静音自动发送（能量VAD）”，避免和服务端 EoU 双触发。

## 4) WebSocket 协议硬规范 / WebSocket protocol spec
- Endpoint: `ws://127.0.0.1:7997/turn`
- Client send JSON text frame:

```json
{"type":"audio","session_id":"a-unique-id-per-stream","audio":"BASE64_OF_FLOAT32_PCM_BYTES"}
```

- Server return JSON text frame:

```json
{"type":"turn_state","session_id":"same-id","state":{"state":"idle|nonidle|speak|blank"},"ts":1710000000.0}
```

关键点 / key points:
- `audio` 必须是 `float32 PCM` 字节做 base64（服务端 `np.frombuffer(..., dtype=np.float32)` 还原）。
- 推荐按 `chunk_size=2560 samples`（16kHz 约 160ms）发送。
- 本项目已在 VAD client 内做 `20ms -> 160ms` 聚合再发送。

## 5) state 与 session_id 约定 / state and session rules
- `idle`: 静默
- `nonidle`: 说话进行中（可触发/维持打断）
- `speak`: 说完（turn 完成触发点）
- `blank`: 过渡态（一般不做最终触发）

session_id:
- 同一条流保持同一个 `session_id`
- 新流/新用户使用新 `session_id`
- 服务端空闲约 60s 会回收 session

