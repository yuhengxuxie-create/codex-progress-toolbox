# Codex 事件监听机制调研

调研日期：2026-08-20（北京时间）

## 结论

当前可行的最小方案是 Codex 官方 `notify`：它专门启动外部程序处理 `agent-turn-complete`，事件自带 `thread-id`、`turn-id` 与最后助手消息。它满足正常轮次结束后即时触发和精确线程过滤，无需解析终端输出、扫描日志或匹配关键词。

进度状态本身不是 Codex 事件字段，因此使用 Responses API 对完整助手结论做结构化语义分类。缺少 API Key 或分类失败时降级为“情况未知”和固定短提示，绝不退回关键词/正则分类，也不复制完整助手回复。

## 官方机制比较

| 机制 | 能否直接用于本项目 | 结论 |
| --- | --- | --- |
| `notify` / `agent-turn-complete` | 是 | 首选触发器；外部程序、字段充分、安装简单 |
| Lifecycle Hooks `Stop` | 否（不直接外发） | `Stop` 可要求 continuation；第一次 Stop 不一定是最终结束 |
| Lifecycle Hooks `SessionEnd` | 否 | 对话归档、关闭或闲置约 30 分钟才触发，不是每轮完成 |
| App Server `turn/completed` | 有条件 | 状态最完整，但仅适合由本工具拥有/订阅的 App Server 会话；不能假定旁路进程收到桌面内部连接事件 |
| transcript/history 轮询 | 否 | 格式非稳定接口，延迟、竞态和误报风险高 |
| 终端文本/关键词正则 | 禁止 | 不是生命周期信号，且无法可靠判断六类语义进度 |

## 1. Advanced Configuration：`notify`

[官方 OpenAI Advanced Configuration](https://developers.openai.com/codex/config-advanced#notifications) 明确说明：

- `notify` 用于在 Codex 发出受支持事件时运行外部程序；
- 目前只支持 `agent-turn-complete`；
- 程序收到一个 JSON 命令行参数；
- 常用字段为 `type`、`thread-id`、`turn-id`、`cwd`、`input-messages`、`last-assistant-message`；
- 官方示例也用 `thread-id` 作为通知分组标识。

这提供了精确线程身份和完成触发，无需对对话文本做任何匹配。

### 保证边界

官方文档没有声称 `agent-turn-complete` 会在失败或取消时触发。项目必须将承诺写成：**正常完成会通知；`failed`/`interrupted` 不保证。**

在桌面宿主的事件顺序中，外部 `notify` 可能先于 App Server 的正式 `turn/completed` 通知开始执行；但它发生在模型停止且 `Stop` continuation 结束之后。因而通知内容可以总结最终助手消息，但不能把此回调等同于覆盖所有终止状态的 App Server 审计事件。

### 固定提交源码审计（不是稳定 API 契约）

为核对上述顺序，调研同时审计了 OpenAI Codex 仓库固定提交 `3b45c29062ff0e76e71c91b6753290400e7fa8da`：[`turn.rs` 的对应流程](https://github.com/openai/codex/blob/3b45c29062ff0e76e71c91b6753290400e7fa8da/codex-rs/core/src/session/turn.rs#L482-L531) 先运行 Stop Hooks；若产生 continuation 就回到采样循环，只有不再 continuation 后才调用 legacy after-agent hook。[`legacy_notify.rs`](https://github.com/openai/codex/blob/3b45c29062ff0e76e71c91b6753290400e7fa8da/codex-rs/hooks/src/legacy_notify.rs#L13-L72) 展示该 hook 组装 `agent-turn-complete` 负载并启动外部命令。

这项源码审计用于解释已观察到的调用顺序，**不应被当成跨版本稳定接口**。稳定契约仍以官方 Hooks、Advanced Configuration 和 App Server 文档为准；升级 Codex 后应重新验证。

## 2. Hooks：为何不直接用 `Stop`

[官方 OpenAI Hooks 文档](https://developers.openai.com/codex/hooks) 说明 Hooks 是 Codex 生命周期的确定性脚本框架。相关事实：

- 每个命令 Hook 从 stdin 接收 JSON；公共字段含 `session_id`、`transcript_path`、`cwd`、`hook_event_name`，轮次 Hook 还含 `turn_id`；
- `Stop` 含 `stop_hook_active` 和 `last_assistant_message`；
- `Stop` 返回 `decision: "block"` 会自动创建 continuation prompt，使 Codex 继续该轮流程；
- `Stop` 的 matcher 当前不生效；
- `SessionEnd` 是会话关闭/归档/闲置等生命周期，不是每轮结束。

因此在 `Stop` 内立刻外发可能早于其他 Stop Hook 的 continuation，造成“一轮多报”或过早报告。官方 `notify` 已处理正常完成边界，优先级更高。

Hooks 还明确提示 `transcript_path` 的格式不是稳定接口，未来可能变化。项目不解析 transcript 来推断进度。

## 3. App Server：完整状态与只读标题

[官方 OpenAI App Server 文档](https://developers.openai.com/codex/app-server) 定义 JSON-RPC 生命周期：

- 连接先 `initialize`，再发送 `initialized`；
- `turn/started` 的状态为 `inProgress`；
- `turn/completed` 的 `turn.status` 可能为 `completed`、`interrupted` 或 `failed`；
- 失败还会携带结构化 `error`；
- `thread/read` 可按 ID 读取持久化线程且不恢复、不订阅它；已设置的用户标题位于 `thread.name`。

### 为什么不把第二个 App Server 当桌面全局监听器

App Server 文档描述的是客户端与其连接所管理/订阅线程的事件流，没有提供“订阅另一桌面进程全部内部会话”的全局 IPC API。单独启动 `codex app-server` 进程不能据此假定会复播桌面应用另一连接上的实时 `turn/completed`。

若要对 `failed`/`interrupted` 也做强保证，必须让本工具成为发起 Codex 轮次的宿主或得到官方全局事件接口。这会改变用户原本在桌面中直接操作对话的工作流，所以当前版本不采用。

当前优先调用 `thread/read` 读取 `thread.name`。这是明确的无副作用元数据用途；失败时依次使用配置覆盖、事件标题和本机 `session_index.jsonl` 的精确 ID 最新标题，最后才使用 ID 兜底。本地索引属于尽力而为的兼容路径，不替代官方 App Server 契约。

## 4. 进度分类

Codex 生命周期事件告诉我们“何时结束”和“是哪个线程”，不会给出产品要求的 `阻塞/路线选择/待人工测试` 等业务语义。采用 [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) 约束 Responses API 的结构化语义判断，原因是：

- 能结合完整结论理解上下文，而非匹配词面；
- 可要求固定 JSON 字段并在本地校验；
- 可把主分类限制为六个推荐状态或“自定义”，并单独约束自定义状态字段；
- 不可用时有确定、简短且不泄露回复正文的“情况未知”降级路径。

本地实现中不得出现针对进度状态的关键字表或正则表达式。正则只可用于配置语法、换行清理等与语义分类无关的机械处理。

隐私方面，启用分类会发送事件类型、标题、最多最后 8 条输入消息（每条尾部 4000 字符）及助手回复尾部 50000 字符到 `OPENAI_BASE_URL`。设置 `PROGRESS_CLASSIFIER_MODE=disabled` 可完全跳过分类请求；此时仅投递“情况未知”和不超过 50 字的固定提示，不外发助手回复。

## 5. AstrBot 原生通知通道

[AstrBot HTTP API](https://docs.astrbot.app/dev/openapi.html) 自 v4.18.0 提供按 UMO 主动发送 IM 消息的接口；[API Scope 对照](https://docs.astrbot.app/dev/openapi-scopes.html) 将它隔离为 `im` 权限。当前 AstrBot v4.27.4 的规范路由为 `POST /api/v1/im/messages`，同时保留文档中的单数兼容入口 `POST /api/v1/im/message`。请求只需目标 `umo` 与四行 `message`，随后由 AstrBot 调用匹配平台实例的 `send_by_session()`。

这比制作插件扩展 Web API 更合适：插件扩展端点要求功能更宽的 `plugin` scope，而核心 IM API Key 只需 `im`。Codex 完成事件仍由本工具的 `notify` 入口捕获；AstrBot 只承担发送，不参与线程筛选和进度分类。

UMO 是 `<平台实例 ID>:<消息类型>:<会话 ID>`，第一段是用户创建的平台实例 ID，不是固定的 `qq_official` 或 `weixin_oc` 字样。必须让目标用户先与机器人互动，并从 AstrBot `/sid` 的真实输出复制 UMO，不能自行拼接。

- QQ 官方私聊：AstrBot v4.24.2 修复了无可用 `msg_id` 时无法主动私聊的问题，因此本项目最低建议 4.24.2，并优先使用 `FriendMessage`；群聊仍受平台场景状态约束。[v4.24.2 发布记录](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.24.2)
- 个人微信：AstrBot v4.22.0 起原生提供基于腾讯 `openclaw-weixin` 的 `weixin_oc` 适配器。普通微信扫码后，用户必须先私聊 ClawBot，使 AstrBot 取得并持久化该用户的 `context_token`；平台没有公开长期主动投递 SLA。[个人微信接入文档](https://docs.astrbot.app/platform/weixin_oc.html)

因此交付采用 `astrbot` 通知 Provider 直接调用核心 IM API，不创建需要额外权限的桥接插件。

## 6. 企业微信与通用 Webhook

[企业微信官方群机器人文档](https://developer.work.weixin.qq.com/document/path/91770) 提供机器人 Webhook，并定义 `markdown_v2` 消息的 `msgtype`/`markdown_v2.content` 请求结构、4096 UTF-8 字节上限以及 `errcode`/`errmsg` 响应。

企业微信和通用 JSON POST Provider 继续作为兼容或灾备通道。通用端支持 Bearer、HMAC 和自定义头。生产默认 HTTPS；HTTP 仅在显式允许且目标为本机回环时用于测试。

## 7. 安装与桌面重启

用户级 `notify` 位于 `~/.codex/config.toml`。安装器必须：

- 幂等写入本工具命令；
- 备份并记录已有 `notify`；
- 卸载时可恢复，且不破坏其他配置；
- 使用绝对路径，避免 Codex 工作目录变化影响入口定位。

Codex/ChatGPT 桌面应用可能长期驻留并缓存配置。安装或卸载后需要**完全退出并重启桌面应用**，这是教程中必须醒目标出的操作。

## 8. 已排除路线

### 解析 JSONL transcript/history

Hooks 官方说明 transcript 不是稳定接口；轮询还会遇到半写入、压缩、延迟和进程崩溃。只适合作为人工排障信息，不作为触发机制。

### 监测 Codex 进程 CPU/输出静默

静默不等于完成，工具等待、审批、网络重试都可能暂时无输出。也无法区分线程。

### 关键词/正则分类

“完成”可能出现在“尚未完成”中，“测试”也不代表“待人工测试”。此路线违反需求红线，且错误率不可接受。

### 非官方个人微信/QQ 客户端自动化

GUI 点按、客户端 Hook 和逆向协议存在登录风控与高维护成本，仍不采用。腾讯官方 QQ Bot 与微信 ClawBot/iLink 不属于这类路线；它们可通过 AstrBot 官方适配器接入，但不能据此控制普通账号现有的任意好友或群。

## 风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| `notify` 不覆盖失败/取消 | 漏报异常终止 | 文档醒目标注；未来改为宿主 App Server |
| 标题尚未生成或 App Server 无法启动 | 显示兜底名 | 显式 ID→标题覆盖；再尝试本地精确 ID 标题索引 |
| Responses API 不可用 | 无法生成语义摘要 | “情况未知”+ 固定短提示，绝不关键词降级 |
| Webhook 超时 | 重试可能重复 | 有限重试；通用端用 thread+turn 去重 |
| AstrBot 未运行或 UMO 错误 | 通知发送失败 | 本机有限重试；`send-test` 先验证；UMO 只从 `/sid` 复制 |
| 微信 `context_token` 缺失或失效 | 微信主动通知失败 | 先私聊 ClawBot；将 QQ 私聊作为更稳定主通道 |
| 配置被覆盖 | 影响用户原 notify | 原子更新、备份、可恢复卸载 |
| 桌面未重启 | 新配置不生效 | 安装器和 README 明确提示完全重启 |
