# 架构说明

## 总体结构

```text
Codex 顶层 notify ───────┐
                         ├─> 本地 SQLite outbox ─> 结构化事件/摘要 ─> 飞书机器人
Codex 只读 SQLite ───────┘                                      │
Desktop wait_threads ── waitingOnUserInput ─────────────────────┤
                                                               │ reply_to_message_id
手机飞书 ──> 飞书 WebSocket 入站事件 ─> open_id/私聊/引用/签名校验 ─┘
                                                               │
                                                               v
                         本地 SQLite 回复队列 ─> Codex app-server RPC
                         Desktop send_message_to_thread（生产）
```

项目服务由四个有明确边界的部分组成：Codex 事件和状态读取、摘要与通知状态持久化、飞书消息渠道、Codex 双向 RPC。消息渠道通过 `MessageChannel` 协议与主服务连接，核心逻辑不依赖具体 IM 平台；微信保留为 legacy 适配器，不进入飞书主路径。

飞书渠道是一个独立线程中的异步 WebSocket 客户端。它不依赖电脑飞书客户端、不操作窗口、不启动本地 HTTP 回调服务；所有本地网络监听仍只限于可选的 Codex `127.0.0.1` 共享网关。

## Codex 事件源

### 正常完成边界

安装器把本项目的本地通知包装器和当前配置文件的绝对路径写入 Codex 顶层 `notify`，保留安装前的命令以便恢复。Codex 发出 `agent-turn-complete` JSON 后，包装器只做快速校验和本地落盘，将原始 payload 写入 `.state` 的 `hook_events` 队列；不在包装器中连接飞书或调用 Codex。`hook_events` 只是项目内部沿用的表名，并不表示使用了 Codex 生命周期 `hooks`。

相同的 `thread_id + turn_id + completed` 组成唯一事件键，SQLite `INSERT OR IGNORE` 保证 `notify` 重复调用只入队一次。包装器失败不会被伪装成“没有进度”，服务启动后仍按有限重试处理落盘或读取错误。

### 结构化状态兜底

轮询器默认每 2 秒打开 Codex 的 `state_5.sqlite` 和 `thread_history_1.sqlite` 的只读连接，并设置 `PRAGMA query_only=ON`。它按 thread ID、标题或路径的精确选择器读取轮次状态：

- `completed`、`interrupted`、`failed` 等直接来自 Codex 状态字段；
- `item/tool/requestUserInput`、审批和文件请求来自 app-server 的结构化 JSON-RPC method；
- Desktop 已有活动任务的人工输入状态来自官方 `wait_threads` 投影的 `activeFlags`；只接受精确的 `waitingOnUserInput`，正文关键词不参与触发；
- 最终答复只按 `thread_turns.final_agent_item_id` 精确读取：兼容当前 `agentMessage + final_answer` 与旧版 `message + assistant + final_answer` 两种结构，且要求正文非空；不会简单取“最后一条消息”；
- `completed` 已投影但最终正文尚未落库时会延迟通知；空正文不会进入摘要模型，也不会被误报为项目阻塞；
- 监控不扫描日志正文，也不使用“完成”“阻塞”等关键词推断状态。

数据库缺失、损坏、锁定或读取失败会进入最多 5 次递增重试；耗尽后服务停止并告警。数据库可读但显式选择器不存在时同样 fail-closed，防止静默监控错误对话。

## 自动监测全局开关

`monitor-settings` 只读查询或原子写入共享 SQLite `meta` 设置；不修改 YAML。旧数据库没有该键时默认开启，保持升级前行为。所有自动发现入口最终经过 `StateStore.discover_auto_monitor()`，因此后台扫描、飞书新建项目会话和个人会话遵循同一个开关。

关闭时，新的自动项不会创建，已有自动项不会删除也不会续期，并由原 `expires_at` 自然退出。手动项仍可添加、移除、接收通知并更新最后活动时间。服务与短命 CLI 使用 WAL 和事务协调；运行中服务无需重启即可在下一轮扫描读取外部设置。

## 飞书消息渠道

### 连接和权限

生产后端使用固定版本的 `lark-channel-sdk`。创建 `FeishuChannel` 时配置：

- `transport.kind = ws`，使用官方 WebSocket 长连接；
- `auto_reconnect = false`，由本服务统一执行最多 5 次退避和熔断；
- `dm_policy = allowlist`，`group_policy = disabled`；
- `allow_from = [target_open_id]`，身份字段仅使用 `open_id`；
- 只启用文本和图片能力，关闭文件、音频、视频、卡片、合并转发和原始事件回传；
- SDK 日志级别为 ERROR，避免凭证和对话正文进入日志。

开放平台基础权限为 `im:message:send_as_bot` 和 `im:message.p2p_msg:readonly`，事件为 `im.message.receive_v1`。图片输入还必须开通 `im:message:readonly`，用于获取用户消息中的资源文件；直接预览式图片回传必须开通 `im:resource`。实现不会申请群聊、通讯录或其他无关权限。

`lark-channel-sdk==1.2.0` 的底层 WS 模块在导入时保存模块级 asyncio loop，因此生产入口必须在本项目创建运行中 loop 之前同步预载 SDK；若检测到该 loop 已被其他模块在运行状态下抢先导入，服务明确拒绝启动并要求重启，不能跨线程冒险复用。该 SDK 停止时还会同时涉及 WS 全局 loop、后台 loop 和 `ExpiringCache` 私有 loop；本项目的兼容层只在主动停止官方 SDK 实例时串行化清理、取消并等待其自有任务。正常 close 1000/1001 不记为错误，其他断线和任务异常仍按原日志及有限重试路径处理；不会修改 `site-packages`。

### 出站消息

主服务先在本地 `notifications` 表生成稳定编号、正文、过期时间和事件键，然后调用渠道发送。发送使用接收者 `open_id` 和稳定幂等 UUID；飞书成功响应必须包含至少一个合法 `message_id`，否则视为结果未知并进入有限重试/停机路径。官方 SDK 可能把长正文拆成多个分片，服务会校验并原子保存返回的全部 `chunk_ids`；任意分片被引用都能准确回到同一通知。`notifications.channel_message_id` 保留首分片用于旧状态库兼容，完整映射写入 `notification_message_ids`。

若发送在“平台已成功”和“本地已标记 sent”之间进程崩溃，重启可能根据稳定编号重复一次；这是明确选择的 at-least-once 出站策略。正文不因崩溃而静默丢失，重复编号可供人工识别。

### 入站引用回复

飞书 WebSocket 事件回调只做轻量过滤和入队，绝不在回调线程调用 Codex。事件必须同时满足：

1. `sender_id == target_open_id`；
2. `sender_is_bot == false`；
3. `chat_type == p2p`；
4. `raw_content_type == text`；
5. 具备入站 `message_id` 和 `reply_to_message_id`；
6. `message_id` 未在进程内重复；
7. 文本正文非空。

主服务再以 `reply_to_message_id` 查询本地出站分片映射，并核对通知编号签名、有效期、一次性消费状态和消息指纹。未引用本工具消息的普通私聊、其他用户、群聊、机器人消息、过期回复、重复回复、ID 与正文编号不一致的回复全部忽略，不自动回复。

绑定阶段使用同一个 WebSocket 机制但不设置白名单：仅在有限时间内接受“指定一次性代码 + p2p + 非机器人”的唯一消息，获得发送者 `ou_...` 后立即断开并写入 `target_open_id`。绑定不会按照昵称或备注猜测身份。

## 状态库、队列和去重

项目目录 `.state/progress-wx.sqlite` 由本服务管理，主要表为：

- `hook_events`：Codex `notify` 原始 JSON 队列（内部兼容表名），按事件键去重；
- `notifications`：通知编号、正文、Codex thread/turn、reply kind、过期时间、飞书 `message_id`、消费和投递状态；
- `notification_message_ids`：一个通知对应一个或多个飞书分片 `message_id` 的唯一映射；
- `processed_turns`：已产生通知的轮次永久去重键，防止服务重启后重复汇报；
- `meta`：状态库 schema 版本。

消息发送前先 `reserve_notification`，出站成功后一次性绑定全部 `message_id`，再标记 `sent`/`processed`。入站回复以带锁事务完成 `peek → consume`；相同消息在内存、SQLite 唯一索引和签名三层去重。schema 6 会无损迁移旧版单 `channel_message_id`，并用独立 `discarded_at` 记录经精确数量和年龄门槛确认丢弃的陈旧测试回复。首次生产启用还会先固定所选对话的当前终态快照，再原子核对并基线待处理 `notify` 数量；快照之后结束的新轮次仍正常通知。

Codex 回复的顺序为：

1. 回复正文进入持久 outbox；
2. 检查服务仍在线、通知未过期且发送者和引用合法；
3. 把固定格式的“已收到”任务放入独立飞书回执队列；回执线程与 Codex 回传线程分离，使用稳定幂等键和有限重试，正常网络目标为 30 秒内到达；
4. 从 Codex Desktop 当前主进程日志读取其动态应用工具管道，只接受 `\\.\pipe\codex-browser-use-<UUID>` 格式；
5. 在同一连接先调用只读 `tools/list`，必须唯一命中 `codex_app/send_message_to_thread`，之后才原子 claim；
6. 只调用一次 `tools/call`，不传 `model` 或 `thinking`，让目标任务保留原设置；明确成功后标记 `delivered`；
7. 提交后的超时、断连、JSON-RPC error、`isError` 或响应 ID 不一致均视为结果未知，保留 `claimed_at` 并停机人工核对，禁止自动重复提交正文。

回执中的 `已收到` 表示安全校验通过并已进入本机回传队列，不表示 Codex 已完成后续轮次。审批或 `requestUserInput` 的回答格式不合法时发送 `未收到` 和重试说明；普通私聊、非白名单消息、群聊及无效引用不进入回执队列。这样既给合法用户快速反馈，也不扩大机器人的响应面。

如果该通知对应由工具启动轮次中的服务端审批或 `requestUserInput`，回复直接写入原 app-server RPC 请求的响应队列；秘密输入 `isSecret=true` 拒绝通过飞书明文传输。对于 Desktop 已经运行的任务，监听器只读取 `wait_threads` 的待输入标志；飞书引用回复通过 `send_message_to_thread` 交给 Desktop，由 Desktop 优先 steer 到仍在运行的当前轮次。

## Codex app-server 与兼容共享网关

新安装与当前生产路径使用 Codex Desktop 官方动态应用工具命名管道，不暴露端口、不重启 Desktop，也不依赖共享 gateway。独立监听线程以最长 10 秒的本地事件等待调用监控最多 8 个已加载任务；`notLoaded` 历史任务不会进入等待目标，并每 30 秒刷新，用户打开后会自动加入。事件可用时会提前唤醒，Desktop 暂不可用时每 5 秒重连而不影响完成通知主循环。`list_threads`、`wait_threads` 和结构化“路线选择”模板都不调用模型。收到引用回复时，即使目标任务仍在运行，`send_message_to_thread` 也会把它作为用户可见的后续消息追加到原任务。旧配置未声明 `reply_transport` 时仍保持 stdio，以保证升级兼容。

历史共享模式和相关脚本只为旧部署兼容保留，不进入默认配置、桌面快捷方式或验收路径。若维护者显式设置 `codex.reply_transport: shared_websocket`，才会启用下列旧流程：

1. 用户从 Explorer 独立打开共享入口并确认；父进程链若属于 Codex 则立即拒绝；
2. 若 Desktop 正在运行，启动器只等待用户正常退出，不强制结束进程；确认、等待和取消阶段不修改或广播用户环境；
3. 确认所有 Codex Desktop 进程自行退出后，启动器创建只监听 `127.0.0.1` 的 gateway；
4. gateway 就绪后再次请求用户确认；确认后用 `ProcessStartInfo(UseShellExecute=false)` 直接启动 AppX 包内的 FullTrust `app\ChatGPT.exe`，且只修改该子进程的环境副本；
5. 监督器核验 Desktop 到固定端口的真实 TCP 连接、PID、创建时间、会话、映像和 AppX 包身份；
6. Desktop 与服务作为两个 WebSocket 客户端连接同一个 gateway；
7. 服务用 `thread/read(includeTurns=true)` 找到唯一 `inProgress` turn，再精确 `turn/steer`。

gateway 的启动授权、generation、owner token、PID marker、Global mutex、`!RunOnce` 恢复和 Windows Job Object 共同防止启动/停止串代。启动前拒绝已有相关用户变量；停止时 Desktop 在线或出现未知回环客户端则拒绝切断。任何 URL、身份或状态歧义均保留现场并停止，不回退到另一实例。

gateway 启动前，启动器把 HKCU 用户环境中现有的标准代理变量镜像到当前进程；变量缺失时可只读投影已启用的固定 WinINET 代理，PAC/自动检测保持原样且不猜测。gateway 子进程继承后恢复启动器原进程环境。WinINET/WinHTTP/TUN 等网络状态从不写入。Codex 回环变量只存在于新 Desktop 子进程的环境副本中；不写用户环境、不安装 RunOnce、不广播 Environment。代理指纹仅供报告外部变化，不作为中止条件。

共享模式不是飞书所必需的；app-server stdio/WebSocket 均属于兼容路径。当前生产配置固定为 `desktop_app_tools`，共享 gateway 的启停或状态文件变化不会改变服务连接身份，也不会触发通知服务停机。

## 重试、熔断和告警

`service.max_attempts` 生产上限为 5，默认退避为 `[1, 2, 4, 8, 16]` 秒。以下操作统一使用该策略：

- 飞书 WebSocket 首次连接和断线重连；
- 飞书消息发送、摘要生成；
- Codex SQLite 读取和 `notify` 队列处理；
- Desktop 工具管道发现、连接和提交正文前的只读 `tools/list` 身份握手；兼容模式下的 app-server 启动、初始化和 `thread/resume`；
- 服务启动阶段的配置、身份和依赖检查。

非幂等的 `send_message_to_thread`、`turn/start` 和 `turn/steer` 不自动重试。任一同类错误耗尽 5 次后，主服务设置全局 stop，停止回复线程、关闭飞书监听、写入日志，优先向已验证飞书用户发送告警；告警也失败时尝试 Windows 顶置错误框。没有“无限重试直到成功”的隐式路径。

## 模块边界

| 模块 | 责任 | 不负责 |
|---|---|---|
| `codex_store.py` | 以只读方式查询 Codex SQLite、精确选择 thread、投影结构化轮次 | 写入 Codex 数据库、关键词推断 |
| `hook_dispatch.py` | 接收 `notify`、快速验证并写本地事件队列（文件名为内部兼容命名） | 连接飞书、启动 Codex |
| `codex_rpc.py` | app-server stdio/WebSocket JSON-RPC、thread/turn、server request | IM 身份校验 |
| `codex_app_tools.py` | 发现并验明 Desktop 官方本地工具管道、`list_threads`/`wait_threads`/`send_message_to_thread`、长度前缀 JSON-RPC、硬超时 | 修改代理/环境、重启 Desktop、扫描任意管道 |
| `codex_gateway.py` | 回环共享 gateway 的启动、监督、恢复和身份检查 | 公网监听、飞书通信 |
| `feishu.py` | 官方飞书 WebSocket、出站 message_id、入站 `reply_to_message_id` 归一化 | 解析 Codex、猜测用户身份 |
| `channel.py` | 平台无关的 `MessageChannel`/`ChannelReply` 协议；兼容 legacy 微信包装 | 平台策略实现 |
| `service.py` | 调度轮询、摘要、outbox、回传、有限重试、熔断告警 | 操作飞书 UI |
| `state.py` | SQLite schema、通知编号、消息关联、消费和崩溃恢复 | 保存 App Secret |
| `summarizer.py` | Codex final 本地摘要或显式配置的兼容端点 | 默认联网调用 AI |
| `secrets.py` | 当前 Windows 用户 DPAPI 加密/解密 App Secret | 读取飞书聊天内容 |
| `installer.py` | Codex notify 的原子安装、恢复和旧命令保留 | 修改旧项目 |
| `config.py` | YAML、环境变量、路径和安全范围校验 | 运行时动态切换账号 |

## 资源和数据边界

空闲监控不创建飞书 UI 对象，不轮询整份日志，不扫描 Codex 全部历史；事件正文只有在进入本地 outbox 和摘要流程时读取。`.state` 可能保存 `notify` payload、通知正文和回复正文，`logs` 可能保存错误类型和诊断上下文，因此目录 ACL 必须仅授予当前 Windows 用户，分享时必须排除这些目录及实际配置文件。

## legacy 微信适配器

`channel.py` 仍保留 `WechatMessageChannel` 包装，供旧配置和历史测试使用；它不改变飞书核心接口。微信适配器依赖桌面 UI 和第三方库，不能提供飞书的服务端 `message_id` 关联，也不能保证不影响手工操作。生产配置默认 `feishu`，没有合法后端时 `probe_only` 只能做只读诊断并拒绝启动。
