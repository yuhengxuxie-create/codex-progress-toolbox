# Codex 重置预警只读 CLI 契约

## 2026-09-08 候选修订（未部署，优先于以下旧规则说明）

schema_version 保持 1，数据库 schema 保持 21，无迁移。每个 latest item 新增：

- `notification_eligible`: boolean；available、合法 A/B 且 expires_at 晚于读取时刻即 true，与飞书投递结果无关。
- `eligibility_reason`: `active` / `expired` / `unavailable` / `invalid`。
- `phase`: `upcoming`（预告）、`announced_available`（官方公告可用）、`watch`（观察）、`legacy`（旧事件）。阶段持久编码于新 event_key 前缀，event_id 继续等于 event_key。

旧 delivery.consumable/terminal/consumer_state 完全保留，客户端不得用其代替新增通知资格。过期事件可回看，不再弹出。官方公告可用不代表用户个人账户到账，建议查看额度/重置券与原公告资格条件。首启不能吞掉有效新提醒；桌面自行持久去重，不能依赖飞书 delivered。

检查时间仍为北京时间 08:00–23:00 整点；每次有界回扫最近24小时，覆盖午夜及来源晚到。已验证精确ID、作者、正文的X帖子复用已有信号缓存；每轮最多配置数量的新候选核验，未完成部分标记 backlog，下轮继续；oEmbed遇429停止进一步核验，不绕过限流。发现候选有界500条。官方父上下文核验失败仍标记来源不完整；forecast 子源故障不再阻止候选发现，但不采用其预测分数。

新规则启用时由worker原子保存规则启用时间到现有来源cursor；首次整点前或夜间启用后的新公告可追赶，重启/失败不会后移该时间。启用以前的已完成公告不自动补发；仍有效的未来预告可识别。A/B事件有效期取证据时间或明确截止时间，指纹取证据身份和阶段，不取扫描时间或预测抓取批次。同一公告同阶段稳定去重；不同帖子不猜测为同一事项。

官方页面保留唯一闭合主文章、必需额度/credits小节、有效表格与正文校验，允许周边节顺序/增删及套餐表头变化（含Standard Business）。`html-v2:`哈希升级先建基线，不能把解析器变更当作官方公告。含today/end of day但无法核定时区的预告仅B，不伪造北京时间执行时刻。过去式明确已重置/发放可判A，分句处理否定、资格条件及无关后续将来时。

status 新增 coverage；各source新增coverage/discovery_error_code；can_alert表示worker具备至少一个可用官方来源，不表示覆盖完整或个人账户已到账。飞书uncertain保留人工核验，不盲重试，且不阻塞其他新事件或桌面通知。

契约版本：`schema_version = 1`。本文档描述本地 FeiShuBOT 的只读状态接口，不是公开 API，也不授权发布或发送消息。

## 命令、范围与退出码

以下命令只读取进程标记、状态文件和数据库；它们不会迁移 schema、领取事件、发送飞书消息或改变投递状态。

```text
python progress-wx.py --config <config.yaml> status
python progress-wx.py --config <config.yaml> reset-alert-status --json
python progress-wx.py --config <config.yaml> reset-alert-latest --json [--limit N]
```

`status` 始终输出 JSON，不接受也不需要 `--json`。`reset-alert-latest` 的 `N` 必须是 1–100 的整数。

| 退出码 | 语义 |
| --- | --- |
| `0` | 命令完成并输出了 schema 1 JSON；对两个 reset-alert 命令而言，`available=false` 仍属于正常的只读结果。 |
| `1` | 仅 `status` 使用：服务进程当前未运行；仍会输出 `running=false` 的 JSON。 |
| `2` | 配置、参数、实例状态、数据库读取或其他受控 CLI 错误；不会把异常内容当作可消费事件。 |

## 当前现场证据

2026-08-31 本机受控迁移到 schema 18 后，以只读方式执行上述命令的一次观察结果如下（已省略 PID、计数及其他运行时细节）：

- `status`：退出码 `0`，`schema_version=1`、`running=true`、`service_state=running`、`channel.state=online`。
- `reset-alert-status --json`：退出码 `0`，`available=true`、`can_alert=true`、`state=partial_failure`；`forecast`、`openai_status`、`openai_codex_docs` 已建立基线，`x_thsottiaux` 因来源 HTTP 429 被独立标为 unavailable。
- `reset-alert-latest --json --limit 10`：退出码 `0`，`available=true`、`items=[]`，首次基线没有倒灌历史预警。

本次来源失败被隔离，未形成预警事件或投递；它不能被写成“X 来源已验证无变化”。消费端仍应依据 `available`、`can_alert`、各来源 health 和 delivery 状态分别判断。

2026-09-04 的未发布候选已跟随 X 官方固定端点迁移，将 oEmbed 白名单更新为 `publish.x.com`，但仍拒绝自动跟随任何重定向。当天只读复测中，syndication 返回 HTTP 429，系统仅从 forecast 取得候选 ID 与发布时间，再逐条通过 X oEmbed 核验精确帖子 ID、作者和正文；目标承诺帖在核验时已超过其约三小时未来窗口，因此决策数为 0，没有补发历史提醒。`openai_codex_docs` 同轮结构校验失败，按单来源不可用隔离，不影响其它来源继续核验。

## `status` 顶层结构

固定字段如下：

| 字段 | 类型/枚举 | 语义 |
| --- | --- | --- |
| `schema_version` | `1` | CLI 契约版本。 |
| `running` | boolean | 进程标记是否仍对应运行中的服务。 |
| `pid` | integer/null | 运行中的 PID；文档、日志和测试不得依赖具体值。 |
| `service_state` | `stopped` / `connecting` / `running` / `reconnecting` | 服务生命周期摘要。 |
| `channel` | object | 飞书连接健康快照，见下表。 |
| `state` | object，可选 | 本地状态统计；键集合可能随 schema 演进，不能把统计字段当作事件正文。 |

`service_state` 的含义是：`stopped` 表示进程不在运行；`connecting` 表示尚未确认连接；`running` 表示服务进程运行（不能单独推断飞书在线）；`reconnecting` 表示曾经连上而当前不在线。连接是否可用应读取 `channel.online`。

`channel` 当前字段为：`state`、`online`、`ever_connected`、`consecutive_failures`、`last_failure_class`、`last_failure_type`、`next_retry_at`、`updated_at`。`state` 的允许值为 `starting`、`connecting`、`online`、`offline`、`failed`、`stopping`、`stopped`、`unknown`；未知值必须归一化为 `unknown`。时间字段为带时区 ISO-8601 字符串或 `null`。失败分类字段是诊断字符串，不应被当作稳定业务枚举。

## `reset-alert-status --json` 结构

顶层固定字段为：

`schema_version`、`available`、`enabled`、`can_alert`、`worker_running`、`worker_started_at`、`worker_heartbeat_at`、`worker_stopped_at`、`state`、`timezone`、`check_hours`、`last_check_at`、`last_success_at`、`next_check_at`、`window_start_at`、`window_end_at`、`pending`、`uncertain`、`last_error_code`、`source_states`。

- `available=true` 才表示 reset-alert 状态可可靠读取；`false` 是不可用/升级所需，不是零事件。
- `enabled` 是配置和持久化开关的结果；`can_alert` 只有在状态可用、已启用、worker 运行且没有 `uncertain` 投递时才为真。
- `state`（对应最近一次运行状态）的枚举为 `never`、`running`、`ok`、`partial_failure`、`interrupted`；`upgrade_required` 是 `available=false` 时的不可用状态。
- 当前时区字段为 `UTC+08:00`，`check_hours` 是本地小时整数数组；时间字段为 ISO-8601 或 `null`。
- `pending` 是未过期的活动投递数，`uncertain` 是平台结果未知、需要人工处理的投递数；二者都不是“本次会发送多少条”的承诺。

每个 `source_states` 元素包含：`source`、`health`、`last_check_at`、`last_success_at`、`last_item_at`、`baseline_ready`、`last_error_code`。固定四个 source ID 为：`forecast`、`openai_status`、`openai_codex_docs`、`x_thsottiaux`。`health` 枚举为 `never`、`ok`、`unavailable`；单个来源失败必须隔离，不得把它伪装成全局无变化。`baseline_ready` 只表示该来源已有可比较基线。

## `reset-alert-latest --json` 结构

顶层为：

```json
{"schema_version": 1, "available": true, "items": []}
```

`available=false` 时，`items=[]` 仅表示当前不可用；只有 `available=true` 且 `items=[]` 才表示没有可返回的保留事件。

每个 item 的字段为：`event_id`、`event_key`、`level`、`evidence`、`window`、`advice`、`created_at`、`expires_at`、`notified_at`、`delivery`。

- `event_key` 是稳定去重键，格式为 `reset-alert:` 加规则版本、级别、来源/证据标识和过期时间共同计算的 SHA-256 前缀；它不是时间戳或递增编号。
- `event_id` 是同一个 `event_key` 的兼容别名，当前必须满足 `event_id == event_key`；不得把两者当作两个事件。
- `level` 当前分类值为 `A` 或 `B`。`evidence`、`window`、`advice` 是展示用字段，不是重新判定事件身份的输入。
- `created_at`、`expires_at`、`notified_at` 是带时区 ISO-8601 字符串或 `null`。

### delivery 与终态

`delivery` 包含持久化字段 `delivery_id`、`state`、`attempt_count`、`next_attempt_at`、`last_error_code`，以及契约字段 `terminal`、`consumable`、`consumer_state`。

| `state` | `terminal` | `consumable`* | `consumer_state` | 可再次 claim |
| --- | ---: | ---: | --- | --- |
| `pending` | false | false | `wait` | 仅在 `next_attempt_at` 到期且事件未过期时 |
| `retrying` | false | false | `wait` | 仅在到期时 |
| `claimed` | false | false | `wait` | 否，正在处理 |
| `delivered` | true | true | `delivered` | 否 |
| `rejected` | true | true | `failed` | 否 |
| `expired` | true | true | `failed` | 否 |
| `uncertain` | true | true | `needs_attention` | 否，禁止盲目重发 |

\* `consumable` 是当前 schema 1 的字面契约：表示已有可供消费者处理的终态结果/应停止自动处理，并不表示“现在可以 claim”。因此只有到期的 `pending`/`retrying` 属于自动 claim 候选；`claimed` 是飞行中，`delivered` 是已记录平台成功，`rejected`/`expired` 是永久关闭，`uncertain` 是平台结果未知并需要人工核查。

任何提交后的未知平台结果都进入 `uncertain`，不能依据网络重试自行判定为失败并盲发。服务重启会释放提交前的 claim；提交后无法确认结果时仍保持 `uncertain`。

## 来源正文与比较边界

`openai_codex_docs` 只比较固定 HTTPS 最终主机上的官方 pricing HTML 的 Codex usage/rate/quota 主正文。抽取必须在 `article#mainContent` 内按产品契约选择八个精确目标 heading 及 `usage-limits`、`credits-overview` 锚点和两张严格表格；导航、脚本、动态时间、属性噪声不能进入正文 hash。固定 host、无重定向、HTTPS、Content-Type 和体积上限、结构异常均是 fail closed 条件。

八个 heading 必须按以下顺序各出现一次（文本逐字匹配，大小写和空白按可见文本规范化）：

1. `What are the usage limits for my plan?`
2. `ChatGPT Voice in Desktop`
3. `What happens when you hit usage limits?`
4. `How does image generation count toward usage limits?`
5. `Where can I see my current usage limits?`
6. `What are tokens and credits?`
7. `What counts as Code Review usage?`
8. `What can I do to make my usage limits last longer?`

`usage-limits` 和 `credits-overview` 各必须是唯一的 `id` 锚点；前者所在表的首行必须精确为 `Model | Plus | Pro 5x | Pro 20x | Business | API Key`，后者所在表的首行必须精确为 `Credits per 1M tokens | Input Tokens | Cached input tokens | Output Tokens`，两表都必须至少有一行数据。目标文章必须唯一且完整闭合；缺失、重复、乱序 heading/锚点、坏表头、无数据行、多目标 article 或无法闭合都拒绝。

正文规范化使用 Unicode NFKC、HTML 实体解码、移除 BOM/软连字符/零宽字符并折叠空白；只保留目标文章内的可见段落、列表和表格结构，以及用于确认共享额度语义的固定前言。元素 class、`data-*`、导航、脚本、样式、隐藏/动态 plan 内容和属性值永不进入 hash。

静态内容不能单独生成 A/B；来源不可用、基线未就绪或正文结构不符合契约时，应保持不可告警并记录来源错误，而不是生成可消费事件。该规则与其他三个来源分别隔离。

## X 官方承诺的核验边界

forecast 的帖子标题、正文、分类或概率都不构成官方证据；它只可提供候选帖子 ID、发布时间和回复关系。候选随后必须通过固定 `publish.x.com/oembed` 端点核对精确 status ID、官方作者和正文，直接回复关系还要独立核验父帖 ID。syndication 被限流时允许 forecast 承担候选发现，但不能跳过 oEmbed。

当 Tibo 官方正文明确承诺：付费 ChatGPT 计划在没有 Astra 权限期间按天获得 banked reset，并给出首张约三小时到账，且计算后的时间仍处于未来 24 小时内时，规则确定性判为 A 级。“约三小时”只放宽时间表达，不放宽作者、帖子身份、额度对象或未来窗口；窗口已过则不生成事件。北京时间每日 08:00 的首次检查从当天 00:00 开始回看，空 X 基线也遵循该范围。事件使用稳定证据指纹去重，重启、重复发现和来源降级不会重复投递。
