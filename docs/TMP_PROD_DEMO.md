# tmp-prod 临时演示产品（国投催授权）

> 分支：`tmp-prod`｜用途：快速演示，非长期正式产品

## 打开页面

服务启动后访问（与 `sip-call` 同挂载前缀）：

- 本地：`http://localhost:7998/tmp-prod`
- 线上：`https://dev.gameday.tech/s2s/tmp-prod`（注意是 **`/s2s/tmp-prod`**，不是站点根路径 `/tmp-prod`）

`/tmp-prod` 与 `/tmp-prod/` 均直接返回页面，避免反代下 307 丢前缀。

## 流程

1. 上传 Excel（格式参考 `docs/tmp.xlsx`，需列：用人单位、姓名、性别、手机号）
2. 点击「开始外呼」
3. 页面轮询批次状态；可下载结果 xlsx

## 剧本要点

- 自称「国投背调组」AI，用人单位默认/取自 Excel「用人单位」
- 注入**本通外呼触发瞬间**的北京时间（`trigger_beijing_at`），据此定「今天」与「今天18点」；开场重写 prompt 不再改时间
- 单通媒体上限 **15 分钟**；收尾请对方挂机
- 未接通：**30 分钟后重呼一次**（重呼算新触发，重新打时间戳）；2 次仍未通 → `fail_reason=未接通`

## 产物

按**单个联系人**对齐：

- `data/tmp_prod/{batch_id}/batch.json` — 含每人 `stereo_path` / `recording_dir`
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
- `GET  /tmp-prod/batches/{id}`
- `GET  /tmp-prod/batches/{id}/export.xlsx`
