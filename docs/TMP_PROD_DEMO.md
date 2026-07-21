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

- `data/tmp_prod/{batch_id}/batch.json`
- `data/tmp_prod/{batch_id}/artifacts/{contact_id}/*_transcript.json`
- 录音：`data/tmp_prod/recordings/{call_id}/`（user/ai/mixed）

## 环境变量（可选）

```bash
TMP_PROD_RETRY_SEC=1800          # 未接通重试间隔，默认 30 分钟
TMP_PROD_ROOT=data/tmp_prod
SIP_RECORD_DIR=data/tmp_prod/recordings
SIP_MAX_CALL_SEC=900
OUTBOUND_DRY_RUN=false           # 真实外呼时关闭
```

## API

- `POST /tmp-prod/batches/upload`
- `POST /tmp-prod/batches/{id}/start`
- `GET  /tmp-prod/batches/{id}`
- `GET  /tmp-prod/batches/{id}/export.xlsx`
