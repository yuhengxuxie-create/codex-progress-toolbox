# 安全说明

## 安全目标

本工具的安全目标是：只把指定 Codex 对话的进度发给一个明确绑定的飞书用户，只把该用户对本工具通知的引用回复送回原对话；任何身份、消息关联、Codex 连接或投递结果不确定时，停止并等待人工处理。

安全策略优先于“尽量继续运行”。因此服务宁可漏发一条并告警，也不猜测账号、窗口、thread 或非幂等写操作的结果。

## 本地网络边界

- 默认通知服务不启动 HTTP、MCP 或公网 WebSocket 监听。
- 飞书使用官方 SDK 的出站 API 和 WebSocket 长连接；电脑不需要飞书客户端，服务不通过 UI 自动化读取聊天。
- 当前生产回复通道使用 Codex Desktop 官方动态应用工具命名管道；无本地 TCP 监听。
- 可选共享 gateway 只允许 `ws://127.0.0.1:<1024-65535>`，拒绝主机名、局域网地址、公网地址、凭证、查询参数和自定义路径。
- 远程摘要端点默认关闭；启用时只接受 HTTPS，或 `localhost`/`127.0.0.1`/`::1` 回环 HTTP，响应体有上限，不接受不安全重定向。
- 所有网络可用性故障受最多 5 次递增重试约束；不会因为网络断开而无限重连或把服务暴露到公网。

飞书网络通信会把机器人消息和必要的协议字段发送给飞书官方服务，这是本方案使用飞书的必然边界。Codex 正文不会发送到飞书以外的服务，除非显式开启外部摘要。

## 飞书应用身份和最小权限

飞书侧使用企业自建应用机器人，不使用个人真人账号的登录 Cookie、二维码或会话凭证。机器人有独立名称和头像，不会挤掉或控制用户的飞书客户端，也不会读取用户的普通联系人聊天。

当前实现只需要在飞书开放平台配置：

- `im:message:send_as_bot`：主动发送进度、告警和审批/人工输入提示；
- `im:message.p2p_msg:readonly`：读取一对一消息事件；
- `im:message:readonly`：获取用户消息中的图片资源；启用图片输入时必需；
- `im:resource`：上传并发送可直接预览的图片消息；启用生成图片回传时必需；
- 事件 `im.message.receive_v1`：接收引用回复。

群聊策略在 SDK 中固定为 disabled，只启用图片媒体能力，文件、音频、视频、卡片、合并转发、原始事件和其他扩展能力关闭。应用不应额外申请通讯录、群管理或其他无关权限。

飞书 SDK 安装清单固定完整 Windows/Python 3.13 依赖闭包并启用 `pip --require-hashes`，同时只接受二进制 wheel；任一版本或 SHA-256 不匹配即停止安装。

## App Secret 和 DPAPI

- `feishu.app_id` 可以写入 `config.yaml`；App Secret 不得写入 YAML、命令行参数、脚本、README、日志或聊天。
- `configure-feishu` 从隐藏输入读取 Secret，并使用 Windows 当前用户 DPAPI 保存到 `.secrets/feishu-app-secret.dpapi`。
- DPAPI 文件绑定当前 Windows 用户和系统保护上下文；复制到另一台电脑或另一 Windows 账号通常不能解密，这是预期的安全行为。
- `.secrets`、`.state`、`logs` 目录和现有子项使用 ACL 只授权当前用户完全控制，安装器会移除继承权限；不要把这些目录加入分享包或云同步目录。
- 发现 Secret 泄露时，应立即在飞书开放平台轮换 Secret，然后在本机重新执行配置，并删除旧 DPAPI 文件。

HMAC 密钥也存放在 `.secrets`，用于本地通知编号和引用关联的签名。HMAC 不是飞书 App Secret 的替代品，也不应离开本机。

## open_id 白名单和绑定

服务只接受一个 `target_open_id`，格式为 `ou_...`。绑定脚本：

1. 生成短时一次性代码；
2. 使用临时 WebSocket 连接等待消息；
3. 只接受 `chat_type=p2p`、非机器人发送者、精确匹配代码的文本消息；
4. 记录该消息的 `sender_id` 作为 `target_open_id`；
5. 立即断开绑定连接，并写入配置。

运行时每条入站消息都重新检查：发送者 ID 是否严格等于白名单、是否为机器人、是否为 p2p、类型是否为文本/图片/富文本、是否有 `message_id`。文本续聊和图片暂存通常还要求 `reply_to_message_id`；只有同一白名单用户、同一 p2p 聊天内已存在未过期图片暂存时，下一条未引用文字才允许作为说明。昵称、备注、手机号和显示名都不是身份凭据。应用配置、目标用户或组织发生变化时服务停止并要求重新绑定，绝不自动选择另一个账号。

## 引用回复关联

主动通知发送成功后，飞书返回一个或多个分片 `message_id`，工具把全部 ID 与本地通知编号、Codex `thread_id`/`turn_id` 绑定在 SQLite 中。入站事件的 `reply_to_message_id` 必须精确命中其中一个出站 ID；否则只记录忽略原因，不进入 Codex。

进入 Codex 前还必须通过：

- `target_open_id` 白名单；
- p2p 聊天和文本类型检查；
- 引用 ID 到通知编号的数据库映射；
- HMAC 签名、有效期和一次性消费检查；
- 消息 ID/聊天 ID/正文/引用 ID 组成的去重指纹；
- 通知仍绑定唯一、未失效的 Codex thread。

工具只消费引用正文或已验证图片暂存的下一条文字说明，不把被引用的原始展示文本当成新的 Codex 指令。普通私聊、非白名单、群聊、未关联图片、已消费消息、过期消息和伪造编号全部忽略，不自动回复。消息正文不要当作安全协议码，除非它对应 app-server 明确定义的审批协议。

## Codex 连接和输入安全

- 监控读取 `state_5.sqlite`、`thread_history_1.sqlite` 时使用 `mode=ro` 和 `PRAGMA query_only=ON`，绝不修改 Codex 数据库。
- 生成图片回传只读取同一 `thread_id`/`turn_id` 下 `imageGeneration` 完成态投影；选定已有会话时也只读取最新一轮，不遍历旧图。只接受 `CODEX_HOME/generated_images/<thread_id>/<item_id>.<png|jpg|webp>` 内、文件名与 item ID 一致、格式签名正确且不超过 30 MB 的普通文件。提取时计算 SHA-256，上传前重读原始 bytes 并再次核对大小和摘要；正文中的普通路径、目录外路径、失败项和被替换的文件不会发送。
- 用户发入的图片必须由飞书官方 SDK 下载并验证，只能落在 `.state/feishu-media` 边界内。同一条引用富文本中的图片和说明会立即一起转交，不写入暂存。纯图片必须引用一条仍可继续的进度通知或会话概览，并按 `sender_id + chat_id` 隔离暂存；有效期 10 分钟，最多 5 张、总计不超过 50 MB。发送文字前再次核对路径边界、MIME 和文件大小；改引用另一任务会替换旧暂存，不会合并两个任务的图片。
- Codex 顶层 `notify` 命令调用的本地包装器只负责把结构化 payload 写入本地队列，不在包装器中执行用户正文或连接公网；它不是 Codex 生命周期 `hooks`。
- 用户级 `PermissionRequest` Hook 只把 Codex 的结构化权限申请写入 `.state/approval-bridge` 的 HMAC 签名队列，并等待唯一飞书白名单用户对对应机器人消息作一次性引用回复。超时或桥服务不可用时不自行批准，而是把决定权交还 Codex 原审批界面。只有请求本身带有明确 `prefix_rule` 时才显示“允许类似操作”；写入 `~/.codex/rules/feishu-approved.rules` 前必须由 `codex execpolicy check` 精确匹配并返回 `allow`，绝不从命令正文猜测规则。
- 监控选择器是精确的 ID、标题或路径，不支持关键词、正则或模糊匹配；选择器未命中或数据库读失败均 fail-closed。
- 当前生产回复通道从受限日志路径发现 Desktop 官方工具管道，先以 `tools/list` 验明唯一工具，再只调用一次 `send_message_to_thread`；活动任务无需重启或等待终态。
- `send_message_to_thread`、`turn/start` 和 `turn/steer` 是非幂等操作。提交后的超时、断连、响应缺失或返回 ID 不一致都视为结果未知，服务停机并要求人工核对，不能自动重发正文。
- app-server server request 必须在原 JSON-RPC 连接上回答；未知 request 拒绝处理。`requestUserInput` 的 `isSecret=true` 不通过飞书明文传输。
- 历史共享 gateway 仅为显式 `reply_transport: shared_websocket` 的旧部署兼容保留，不进入默认配置或桌面入口。其启动/恢复仍通过 generation、nonce、owner token、PID 创建时间、Desktop AppX 包身份、Global mutex 和 Job Object 绑定，拒绝接入其他实例。
- Windows 共享 gateway 只接受可执行的 Codex CLI 或由显式 npm shim 精确解析出的唯一 native CLI；若 PATH 只命中受保护的 WindowsApps Desktop 包入口，会在创建子进程前明确失败关闭。
- 共享启动器不修改 WinINET/WinHTTP 代理、Clash 端口、路由、DNS、网卡或 TUN。它优先把当前用户已存在的四个标准代理环境变量临时镜像到 gateway 子进程；缺失时可只读投影已启用的固定 WinINET 代理，但不会展开 PAC/自动检测。调用后逐项恢复当前进程，缺失项会真正删除。代理注册表与用户环境只做 SHA-256 指纹比较，不写日志值；前后不一致时失败关闭且不自动回滚外部程序的修改。
- 临时 `CODEX_APP_SERVER_WS_URL` 和 owner token 只在本代 token/预期回环 URL 精确匹配时直接删除对应 HKCU 环境值。直接注册表删除用于保证名称本身消失，并避开同一进程用户环境枚举可能滞后的差异。

## 重试和熔断

`service.max_attempts` 生产上限为 5，默认退避为 `[1, 2, 4, 8, 16]` 秒。连接、发送、Codex 读取、摘要和启动等可重试操作耗尽后：

飞书返回的明确不可重试错误不进入五次退避。例如图片上传缺少 `im:resource`（或 `im:resource:upload`）时只尝试一次，发送权限提示并暂停该图片在本次运行中的交付；正文通知保持可引用，其他监测继续运行。图片事件不会标记完成，权限开通并重启后可按稳定幂等键补发。

1. 设置全局停止标记；
2. 停止回复线程和飞书监听；
3. 写入最近 7 天日志；
4. 若飞书仍在线，向已验证的 `target_open_id` 发送一次告警；
5. 告警失败时尝试 Windows 顶置错误框。

同一个错误不会在后台无限重试，也不会通过切换飞书用户、切换 Codex thread 或切换微信窗口来“自愈”。

## 本地数据和日志

`.state/progress-wx.sqlite` 可能包含：

- Codex `notify` 原始 payload；
- 最终答复和通知摘要；
- 飞书 message ID 与 Codex thread/turn 关联；
- 用户引用回复正文和投递状态。
- 未过期的图片暂存路径、MIME、SHA-256、大小和原始飞书消息 ID。

`.state`、`.secrets`、`logs` 和实际 `config.yaml` 均视为敏感本地数据：

- 不提交版本库；
- 不上传公共网盘、工单或聊天；
- 不作为朋友分享包的一部分；
- 只有当前 Windows 用户有读写权限；
- 日志保留 7 天，状态清理不能删除永久去重键或未确认的结果未知记录。

日志应记录错误类型、操作名、thread/turn/消息 ID 的必要部分和重试次数；不得记录 App Secret、完整消息正文、绑定码或远程摘要 API Key。排障时优先提供脱敏后的日志和配置结构，不要直接复制数据库。

## 外部摘要

生产默认 `summary.mode: codex_cli`，通过已登录的官方 Codex CLI 使用独立的 `gpt-5.6-luna`、`low` 推理档生成正常结束的进度报告；主任务模型和推理档不会被读取或改写。分类子进程使用临时目录、只读沙箱、`--ephemeral`、`--ignore-user-config` 和 `--ignore-rules`，并移除 API Key 环境变量，只提交对话标题、结构化状态、最多 12000 字符的最终答复尾部和最多 2000 字符的结构化错误，不读取工作区。失败、打断、审批和 `waitingOnUserInput` 等已有可靠结构化映射的事件不消耗模型额度；Desktop `list_threads`/`wait_threads` 监听本身也是本地工具调用，不调用模型。相同事件在调用前持久化去重。启用 `openai_compatible` 前仍需确认 Codex 最终答复可以发送到所配置端点，并设置合理的 `min_interval_seconds`。

如果语义分类不可用，按 5 次策略停止并告警，不自动改用一个未授权的公共服务。完全不希望消耗模型额度时可使用 `codex_final` 或 `disabled`，此时正常结束只能保守显示 `*/*`，详情由结构化状态生成。

## 共享和异地安装

分享项目给朋友时只分享源码、示例配置、脚本和文档。每位使用者应：

1. 在自己的飞书组织创建或获得授权的自建应用；
2. 自己输入 App Secret，让本机 DPAPI 保存；
3. 用自己的手机账号重新绑定 `open_id`；
4. 自己选择 Codex thread；
5. 完成端到端测试后才启用自启。

不能共享源电脑的 DPAPI 文件、HMAC 密钥、`target_open_id` 配置、Codex 数据库或状态库。异地安装细节见 [REMOTE_INSTALL.md](REMOTE_INSTALL.md)。

## legacy 微信说明

微信适配器仅用于兼容旧配置，依赖桌面 UI 自动化，并可能与用户手工操作共享窗口；它不是飞书安全模型的一部分，不能提供飞书的服务端 `message_id`/`reply_to_message_id` 关联。无合法生产后端时，`probe_only` 只允许只读诊断并拒绝启动。不要为了启用飞书而登录、退出或切换微信账号。
