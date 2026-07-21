# 催授权场景话术约束测试手册（当前已实现范围）

> Scope: test only implemented conversation constraints in prompt/system_role.  
> 范围：仅测试当前已实现的「接通后话术约束」，不包含未接通重呼/改约调度/结构化抽取。

## 1. 测试目标 Test Goals

验证以下能力已经生效：

1. 情景分支约束（opening / trust_crisis / not_received / claims_filled 等）已注入到每通 `system_role`
2. 禁令黑名单已注入（禁止威胁施压、禁止编造后台步骤、禁止盲指引公众号等）
3. 冒烟降级口径已注入（重业务问题降级为记录诉求 + 顾问跟进）
4. 身份台词族切换已生效（`BRAND_SELF` vs `CLIENT_IDENTITY`/`CHANNEL_BRAND`）
5. 任务槽位映射可驱动话术裁剪（如 `has_email` / `has_fallback_sms`）

---

## 2. 前置条件 Preconditions

1. 服务可启动并可访问 `backend/server.py` 暴露的 outbound 接口
2. 可访问测试网页：`frontend/sip_call.html`（例如 `http://127.0.0.1:8000/sip-call`）
3. 设置环境变量（测试建议）：
   - `OUTBOUND_DRY_RUN=true`（不真实拨号）
   - `OUTBOUND_EXPOSE_PROMPT=true`（允许查看 `system_role`）
4. 准备 API Key（若配置了 `PARTNER_API_KEY`）

---

## 3. 核心测试路径（网页） Core Test Flow (Web Page)

### Step A. 在网页填写基础信息

在网页中至少填写/选择：

- 被测手机号（外显号由服务端号池自动分配，页面无需选择）
- 候选人称呼、委托方/自称品牌
- 身份模式（`CLIENT_IDENTITY` / `CHANNEL_BRAND` / `BRAND_SELF`）
- 发送渠道、发送时间、手机尾号、完成时限
- `已配置邮件渠道`、`已配置兜底短信`
-（可选）预留邮箱、公众号名、兜底短信正文、回拨号码、补发提醒渠道
- API Key（若服务开启鉴权）

期望 Expected:
- 页面“开场白预览”正常刷新（表示 `/preview-opening` 工作正常）

### Step B. 发起 dry-run 呼叫任务

点击 **发起外呼**。

期望 Expected:
- 状态区显示 `status=queued`
- 返回 `call_id`
- 若 `OUTBOUND_DRY_RUN=true`，不会真实外呼

### Step C. 查看本通注入 prompt（接口校验）

拿到 `call_id` 后，用接口检查最终 `system_role`：

```bash
curl "http://127.0.0.1:8000/outbound/v1/calls/<call_id>/prompt" \
  -H "X-API-Key: <YOUR_API_KEY>"
```

期望 Expected:
- `system_role` 中出现情景段标题（如 `opening`, `not_received`, `trust_crisis`, `close`）
- 含禁令区（禁威胁施压、禁编造后台步骤）
- 与网页当前选择一致（身份模式、渠道裁剪、兜底短信策略）

---

## 4. 网页测试矩阵 Test Matrix (Web Form Driven)

每条用例都走「网页填参 -> 发起 -> 用 `call_id` 查看 prompt」方式验证。

### 4.1 身份台词族 Identity Mode

#### Case I1: BRAND_SELF
网页操作：
- 身份模式选 `BRAND_SELF`
- 我方品牌填 `芝麻背调`
- 委托方/自称品牌填 `闪购`

检查点：
- 开场应包含“我是芝麻背调的 AI 语音助手，受闪购委托...”
- 应包含“第三方/受托”相关核验口径

#### Case I2: CLIENT_IDENTITY（默认）
网页操作：
- 身份模式选 `CLIENT_IDENTITY`
- 委托方/自称品牌填 `闪购`

检查点：
- 开场应以“我是闪购的 AI 语音助手...”表达
- 不应出现“受委托/第三方背调机构”自我说明

#### Case I3: CHANNEL_BRAND
网页操作：
- 身份模式选 `CHANNEL_BRAND`
- 渠道品牌名填 `国投人力`

检查点：
- 自称应使用“国投人力”
- 口径应复用白标族（不出现受托/第三方自称）

### 4.2 渠道级联裁剪 Cascade Pruning

#### Case C1: has_email=false
网页操作：
- 取消勾选「已配置邮件渠道」
- 发送渠道填 `短信`

检查点：
- `not_received` 级联中不应要求查邮件

#### Case C2: has_fallback_sms=false
网页操作：
- 取消勾选「已配置兜底短信」
- 公众号名称留空
- 兜底短信正文留空

检查点：
- `not_received` 中应跳过兜底短信/公众号现场指引，直接收敛到反馈补发路径

#### Case C3: has_fallback_sms=true + fallback_sms_body 非空
网页操作：
- 勾选「已配置兜底短信」
- 兜底短信正文填入示例文案

检查点：
- prompt 应强调“按兜底短信正文指引，不可脱离短信编步骤”

### 4.3 禁令与降级 Guardrails

#### Case G1: claims_filled 禁编造进度
检查点：
- prompt 中明确“禁止编造后台差某一步”
- 采用询问式排查电子签/人脸

#### Case G2: hard interrupt 让路
检查点：
- `not_convenient` 下明确开车/医院等需立即收线，不可继续追问

#### Case G3: heavy_business 冒烟降级
检查点：
- 讲解/意愿阻力/填写口径问题需降级为“记录诉求 + 顾问跟进”

---

## 5. 建议真人通话冒烟（可选） Optional Live Call Smoke

> 这一步同样建议从网页发起，只是关闭 dry-run 并接入真实 SIP/被叫环境。

建议做 2 通：

1. `CLIENT_IDENTITY` 模式：测试开场自称、信任危机回答
2. `BRAND_SELF` 模式：测试受托口径、未收到级联排查

主观验收点：
- 有无复读/威胁式催促
- 对“没收到”是否按级联自然推进
- 对“我在开车”是否及时让路

---

## 6. 当前不在本手册范围 Not In Scope

以下属于后续编排层/增强项，本手册不判失败：

1. 未接通 30 分钟自动重呼
2. 改约时间到点自动二次外呼
3. 挂机后 §8 全量结构化结果抽取
4. `result_detail` webhook 扩展
5. 工具调用（实时查状态、当场补发）

---

## 7. 快速通过标准 Quick Pass Criteria

满足以下 5 条，可判「本阶段实现通过」：

1. 可通过网页创建 dry-run 呼叫并获得 `call_id`
2. 可通过 `/calls/{call_id}/prompt` 查看完整 `system_role`
3. identity 模式切换时，开场与身份核验口径正确变化
4. `has_email` / `has_fallback_sms` 可影响 `not_received` 级联内容
5. prompt 明确包含禁令与冒烟降级条款

