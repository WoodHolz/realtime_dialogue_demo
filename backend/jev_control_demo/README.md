# Jev SOP control demo

This branch adds an independent experiment to the existing `t2s` path:

`LiveKit VAD -> Doubao ASR -> Jev branch selection -> SOP guidance + user text -> Doubao end-to-end model`

Jev only performs closed-set selection. Code owns stage state and advances it from explicit branch metadata.

## Run

Install the repository requirements in its virtual environment, then add to `.env`:

```dotenv
TYPESAFE_API_KEY=...
JEV_MODEL=jev-latest
```

Start the existing server and open `/urge-auth/t2s`. It connects only to `/ws/urge-auth/t2s`; Jev is selected by this server route, not by a global switch or a flag accepted by the old `/ws`. Existing `/t2s` and `/t2sbg-voice` pages continue to use `/ws` and keep their previous behavior. The dedicated WebSocket config may set `jev_initial_stage` or `jev_model`.

Each final ASR result emits `jev_control_decision` with the SOP, stage transition, confidence, and latency. Selection failure emits `jev_control_error` and fails closed, so uncontrolled text is not forwarded to Doubao.

The demo represents a first outbound call. After the session is ready, Doubao proactively speaks the opening line. Jev starts in `OPENING`, classifies identity/availability, and only advances to `DELIVERY` after the user confirms they are the candidate and can continue.

## Stage completion

- Delivery facts advance to unfinished, channel search, or backend check.
- Finding a notification returns to unfinished-reason handling; a missing channel advances to the next channel.
- Troubleshooting SOPs hold the stage until a later user answer selects a progress branch.
- Non-terminal global side branches preserve the main stage. Safety, refusal, abandonment, and complaint close this demo flow.
- An accepted completion commitment closes the main flow.

## Asynchronous progress LLM

The dedicated endpoint enables asynchronous progress assessment. Jev selection keeps
the current stage; after TTS ends, a background task assesses the user message,
complete assistant text, SOP goal, and current status. The model proposes boolean
facts, completion, an evidence quotation, and a pending question. Code checks the
quotation and turn ID, then applies the existing transition table. Stale results
are discarded, interrupted output cannot complete an SOP, and failures hold the
current stage. Safety/refusal terminal global branches still close immediately.

Configure `JEV_PROGRESS_API_BASE`, `JEV_PROGRESS_API_KEY`, and
`JEV_PROGRESS_MODEL` for an OpenAI-compatible Chat Completions endpoint. The default
model is `qwen3.6-flash` with thinking disabled and temperature 0, matching the
existing intent-evaluation profile. Credentials reuse `DASHSCOPE_API_KEY` (then
pipeline settings), and the endpoint reuses `DASHSCOPE_BASE_URL` (then pipeline
settings). The browser's
Jev panel shows pending/applied/discarded/error progress events. No additional
Python dependency is required. This assessment runs after generated audio ends;
it does not verify browser playback delivery. A new user turn never waits for the
analysis; if analysis is late, that turn uses the last confirmed stage.

State remains in memory per WebSocket. Persistence, a side-path stack, and backend
tools are outside this demo. Finding a callback time does not yet create a callback
task or a dedicated callback branch.
