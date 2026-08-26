# Webhook 契约

## 共同正文

所有 Provider 的 `text`/`content` 都是 UTF-8 编码的固定四行字符串：

```text
对话名称：<title>
当前进度：<status>
进度详情：<detail，最多50个Unicode字符>
本条消息时间：YYYY-MM-DD HH:mm:ss（北京时间）
```

动态值中的 CR/LF 和控制字符会被折叠为空格。正文不使用 Markdown；状态最长 12 个 Unicode 字符，详情最长 50 个 Unicode 字符。

`status`/结构化 `progress` 优先为：

- `停滞`
- `阻塞`
- `路线选择`
- `完成`
- `待人工测试`
- `待审批`

六个状态都不准确时，可使用语义分类器拟定的自定义短状态。分类不可用时固定为“情况未知”。所有 `detail/details` 都在本地强制限制为 50 个 Unicode 字符。

## 通用 HTTP Provider

### 请求

```http
POST /configured/path HTTP/1.1
Content-Type: application/json; charset=utf-8
User-Agent: codex-progress-notify/1.2
```

```json
{
  "schema_version": "1.0",
  "event": "codex.turn.completed",
  "event_id": "d5f6f5b0-0000-5000-8000-000000000000",
  "conversation_id": "thr_123",
  "conversation_name": "支付回调",
  "progress": "待人工测试",
  "details": "代码和自动化测试已完成，需要在沙箱验证。",
  "sent_at": "2026-08-20T21:08:35+08:00",
  "timezone": "Asia/Shanghai",
  "text": "对话名称：支付回调\n当前进度：待人工测试\n进度详情：代码和自动检查已完成，需要在沙箱验证。\n本条消息时间：2026-08-20 21:08:35（北京时间）"
}
```

字段约束：

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | string | 是 | 当前固定为 `1.0` |
| `event` | string | 是 | 固定为 `codex.turn.completed`；表示本工具的正常完成语义边界 |
| `event_id` | string | 是 | 由 `conversation_id` 与 Codex `turn-id` 确定性生成的 UUIDv5 |
| `conversation_id` | string | 是 | Codex 完整线程 ID |
| `conversation_name` | string | 是 | App Server 标题或兜底名称 |
| `progress` | string | 是 | 六个推荐状态、自定义短状态或“情况未知” |
| `details` | string | 是 | 最多 50 个 Unicode 字符的快速预览详情 |
| `sent_at` | string | 是 | 带 `+08:00` 的 ISO 8601 发送时间 |
| `timezone` | string | 是 | 固定为 `Asia/Shanghai` |
| `text` | string | 是 | 严格四行纯文本完成提醒 |

接收端返回任意 2xx 即视为接收成功；其余状态按配置决定是否重试。响应正文不要求特定结构。

### 鉴权

`auth_type=none`：不额外增加鉴权。

`auth_type=bearer`：

```http
Authorization: Bearer <PROGRESS_WEBHOOK_BEARER_TOKEN>
```

`auth_type=hmac-sha256`：先取发送时 Unix 秒级时间戳，再对以下字节计算 HMAC-SHA256：

```text
ASCII(timestamp) + b"." + 实际发送的 UTF-8 JSON 字节
```

发送两个请求头：

```http
X-Progress-Timestamp: <timestamp>
X-Progress-Signature: sha256=<hex digest>
```

消费者必须以头中的时间戳和原始请求字节验签，使用常量时间比较，并拒绝超出自身容忍窗口的旧时间戳；不要解析后重新序列化再验签。

`headers_json` 可增加供应商自定义头，但不得覆盖 `Content-Type`、`Content-Length` 或由本工具生成的鉴权头。

## 企业微信 Provider

请求体遵循企业微信群机器人 `markdown_v2` 消息，`content` 按 UTF-8 计最多 4096 字节：

```json
{
  "msgtype": "markdown_v2",
  "markdown_v2": {
    "content": "对话名称：支付回调\n当前进度：待人工测试\n进度详情：代码和自动检查已完成，需要在沙箱验证。\n本条消息时间：2026-08-20 21:08:35（北京时间）"
  }
}
```

典型成功响应：

```json
{"errcode":0,"errmsg":"ok"}
```

判定规则：必须同时满足 HTTP 2xx、响应为 JSON 对象且 `errcode == 0`。HTTP 200 但 `errcode != 0` 是业务失败。

Webhook URL 中的 `key` 是凭据。日志、异常和测试快照必须把查询串脱敏。参考[企业微信官方群机器人文档](https://developer.work.weixin.qq.com/document/path/91770)。

## 重试与幂等

通用接收端应以 `event_id` 作为幂等键；它由 `(conversation_id, turn-id)` 确定性派生。企业微信负载没有扩展元数据字段，网络超时后的重试可能产生重复消息；这是 at-least-once 投递的固有限制。

建议：

- 连接错误、超时、HTTP 408/429/5xx：有限重试；
- 其他 4xx：不重试；
- 企业微信非零 `errcode`：视为业务失败，不由 HTTP 层自动重试；
- 最大尝试次数由 `PROGRESS_WEBHOOK_MAX_ATTEMPTS` 控制。

## 本地测试

测试时只允许回环目标，并显式设置：

```powershell
$env:PROGRESS_ALLOW_HTTP_LOCALHOST = "true"
$env:PROGRESS_WEBHOOK_URL = "http://127.0.0.1:<随机端口>/hook"
```

自动化测试使用进程内 `http.server.ThreadingHTTPServer` 捕获请求，不向互联网、企业微信或 OpenAI 发送数据。
