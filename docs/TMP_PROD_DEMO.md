# tmp-prod 临时演示产品（芝麻背调催授权）

> 分支：`tmp-prod`｜用途：快速演示，非长期正式产品

## 打开页面

服务启动后访问（与 `sip-call` 同挂载前缀）：

- 本地批量：`http://localhost:7998/tmp-prod`
- 本地单人：`http://localhost:7998/tmp-prod/single`（拨号 + TTFS 延迟可视化）
- 线上批量：`https://dev.gameday.tech/s2s/tmp-prod`
- 线上单人：`https://dev.gameday.tech/s2s/tmp-prod/single`

`/tmp-prod` 与 `/tmp-prod/`、`/tmp-prod/single` 与 `/tmp-prod/single/` 均直接返回页面，避免反代下 307 丢前缀。

单人页走 `POST /tmp-prod/single/calls`（内部建 1 人批次 + harness）。通话结束后可回放双声道录音并查看 ASR / AI 转写；改约/未接通会入队到点再拨。轮询：`GET /tmp-prod/single/batches/{batch_id}`。录音仍可用：`GET /outbound/v1/calls/{id}/recording/stereo`。

## 流程

1. 上传 Excel（格式参考 `docs/tmp.xlsx`，需列：用人单位、姓名、性别、手机号）
2. 点击「开始外呼」
3. 页面轮询批次状态；可下载结果 xlsx

## 剧本要点

- 自称「芝麻背调」AI，用人单位默认/取自 Excel「用人单位」
- 注入**本通外呼触发日**（北京时间）作为「今天/明天」唯一日历源；默认催办「今天 18:00」
- 对方推到明天：模型按阶梯主动敲定 **明天 12:00 → 14:00 → 对方给确切时刻**；拿不到小时级则按**天级**回写交付顾问
- 单通媒体上限 **15 分钟**；收尾请对方挂机
- **Harness 编排**（通话外，不长 sleep）：
  - 未接通：写入 `NextAction(retry_no_answer)`，**到期再启新外呼**（默认 30 分钟后，最多 2 次）
  - 不方便且锁到小时级 `promised_time`：写入 `callback_reschedule`，到点新拨并注入 `callback_context`（改约短开场）
  - 动作队列：`data/tmp_prod/harness/actions.json`；进程重启可恢复 pending
  - 完成承诺（`received_will_complete`）MVP **不**自动催办再呼

## 产物

按**单个联系人**对齐：

- `data/tmp_prod/{batch_id}/batch.json` — 含每人 `stereo_path` / `recording_dir` / `pending_action_id` / `next_retry_at`
- `data/tmp_prod/harness/actions.json` — 未接通重试 / 改约回电队列
- `data/tmp_prod/{batch_id}/contacts/{contact_id}/{call_id}/stereo.wav` — 双声道（L=用户，R=AI）
- `data/tmp_prod/{batch_id}/artifacts/{contact_id}/*_transcript.json`
- 原始 SIP 落盘：`data/tmp_prod/recordings/{call_id}/`（会再拷贝到上面 contacts 目录）

### 清理录音

可以直接删，不影响服务进程（已结束的通话不再读这些文件）：

```bash
# 清全部临时演示录音与批次对齐拷贝
rm -rf /home/ubuntu/realtime_dialogue_demo/data/tmp_prod/recordings
rm -rf /home/ubuntu/realtime_dialogue_demo/data/tmp_prod/*/contacts

# 只清某一通
rm -rf /home/ubuntu/realtime_dialogue_demo/data/tmp_prod/recordings/<call_id>
```

删掉后网页/xlsx 里旧的 `stereo_path` 会变成失效路径，属正常。正在进行中的通话不要删对应目录。
## 环境变量（可选）

```bash
TMP_PROD_RETRY_SEC=1800          # 未接通重试间隔，默认 30 分钟
TMP_PROD_HARNESS_POLL_SEC=5      # harness 扫描 due_at 间隔（秒）
TMP_PROD_MAX_CONCURRENT=60       # 批次内同时外呼人数（默认跟随 OUTBOUND_MAX_CONCURRENT）
TMP_PROD_ROOT=data/tmp_prod
SIP_RECORD_DIR=data/tmp_prod/recordings
SIP_MAX_CALL_SEC=900
OUTBOUND_MAX_CONCURRENT=60       # SIP 层全局并发上限
OUTBOUND_DRY_RUN=false           # 真实外呼时关闭
```

批次默认**并发外呼**（不是等上一通挂机再打下一通）；`stagger_sec` 只是启动间隔（默认 0.5s）。
## API

- `POST /tmp-prod/batches/upload`
- `POST /tmp-prod/batches/{id}/start`
- `GET  /tmp-prod/batches/{id}`（含 `harness_actions` 队列）
- `GET  /tmp-prod/batches/{id}/export.xlsx`
- `POST /tmp-prod/single/calls`（单人测试，走 harness）
- `GET  /tmp-prod/single/batches/{batch_id}`（单人轮询：contact + call + harness）
- `GET  /tmp-prod/calls/{call_id}/export.xlsx`
