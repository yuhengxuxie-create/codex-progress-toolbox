# Guardian 本地协议 v1（候选实现，未部署）

所有命令通过 `progress-wx.py --config <配置绝对路径> <命令>`。桌面负责显示与显式操作；Windows 监督负责有限恢复；后端负责权威意图、实例身份、消息和队列。只有 guardian 持有飞书长连接，worker 不加载飞书凭据、不另开连接。

## CLI

| 命令 | 语义 |
|---|---|
| `start` | 显式启动通信守护和业务，允许从 exited 重开；维护中拒绝。退出 0 仅 worker ready 且渠道在线，失败 1 |
| `stop --timeout N` | 持久 stopped，协作停业务、保留救援通信。停止 0，超时 2 |
| `guardian-status --json` | 硬只读完整 JSON，含未启动状态，正常解析退出 0 |
| `guardian-run` | 内部常驻通信控制循环，独立实例锁 |
| `guardian-stop --timeout N` | 持久 exited，业务退出后关闭救援 |
| `guardian-maintenance --enter [--shutdown-guardian] --timeout N` | 保存原意图进入维护、停业务，可停 guardian 供替换代码 |
| `guardian-maintenance --leave` | 恢复原意图，按需重新启动 guardian；原 exited 不启动 |
| `guardian-recover --expected-pid N --expected-creation-time T --reason crashed\|unresponsive` | 监督恢复入口，核实身份、心跳、意图，持久恢复事件；无记录时仅 0/0 可表示无旧实例。maintenance/exited 拒绝 |
| `worker-recover --expected-pid N --expected-creation-time T` | 仅本机显式恢复已证明无响应的业务实例；不自动杀业务，不清除 uncertain。返回 0 仍要求新业务实际就绪 |

旧 `status` 保留顶层 schema_version=1/running/pid/service_state/channel/state 并附加 guardian 状态。普通 start 不依赖桌面重启器，前台 run 也通过 guardian 控制；内部 worker-run 需独立启动授权。

## guardian-status JSON

顶层 schema_version=1、available、desired_state（running/stopped/maintenance/exited）、last_error_code、recovery_required、supervisor_state_path。

- guardian：running、pid、creation_time、generation、heartbeat_at、healthy、control_directory。
- worker：running、pid、creation_time、generation、heartbeat_at、ready、state（starting/ready/stopped/failed/unresponsive）。
- channel：online、state。
- maintenance：active、previous_desired_state。
- windows_supervisor：可选独立 windows-supervisor.json 的只读内容，Windows 层唯一写；字段 schema_version/circuit_open/attempts/next_attempt_at/last_check_at/last_error_code/healthy_since。
- queue：独立通信出站的各状态数量，不包含消息正文。

时间为 UTC Unix 秒，未知 null。generation 不透明。guardian 主循环每约 2 秒推进心跳，15 秒无推进不健康；worker 主业务循环心跳 30 秒阈值，初始化宽限 90 秒。未来心跳超过 10 秒视为 clock_skew，不据此强杀。PID 必须与创建时间一致，不信任 PID 单值。

## IPC、发送及恢复

独立目录 `<业务database父目录>/guardian`，Windows ACL 仅当前用户和 SYSTEM；使用独立 SQLite JSON 持久 IPC，无新增公网/回环监听、无 pickle。业务数据库不能成为救援命令依赖。

入站先落持久队列，再交 worker；每个 worker generation 可重交未最终清理的事件，业务层使用原 message_id 幂等。不能因内存入队就删 IPC 记录。发件先按稳定 key+payload hash 入队，平台提交后进程中断标记 uncertain，不自动重发；明确尚未提交才退避重试。队列、单载荷、保留时间均有界，满队列明确错误。

系统启动/停止/失败/连接恢复由确定性状态转换创建持久通知，不交给 Luna。新 worker generation 实际初始化/业务心跳/渠道就绪后才生成启动成功；重复 start 不产生新 generation。维护状态合并提示，已退出不被监督重新打开。完全断网或关机不能保证同机远程通知或开机。

生命周期消息可通过同应用 REST 在 WebSocket 断开但 HTTP 可达时发送，没有第二个长连接。普通业务仍等待 WebSocket 可用。两条发送路径共用平台 UUID 算法，明确可重试的拒绝才退避；结果未知冻结。连接恢复时将尚未送出的旧离线提示合并为恢复消息。

超过1MiB的出站媒体使用私有media-blobs文件与JSON引用，校验路径/重解析点/大小/哈希；不把base64正文复制进SQLite。blob总量256MiB/128文件（包括临时文件），跨进程文件锁和DB事务按固定顺序串行配额/入队/清理。多个outbox key可以共享同内容，pending/submitted/cancelled/uncertain引用均保留；无引用终态可回收，创建后未入队的孤儿24小时后清理。普通文件依据飞书30MB上限，以30,000,000字节保守校验，空文件拒绝；图片仍遵守图片专用限制。

Windows原登录自启选择由监督安装器保留；guardian读取Token AuthenticationId与系统启动时间形成登录标识。仅已有标识发生变化、desired_state=running且旧worker不活跃时，持久创建一次新启动意图。首次标识缺失、读取失败、同登录内业务崩溃或stopped/exited/maintenance均不会推断启动；同登录启动失败不被监督无限重试。此判断不依赖可复用的SessionId或PID，也不执行任何电源操作。

通信库写入失败时，适配器把完整入站消息交给独立私有恢复目录，最多128条/64MiB；成功持久化后停止守护，由监督恢复，再核验原消息身份、哈希、24小时期限和附件有效性后重放。不能核验的消息生成持久拒绝提示。恢复目录也不可写时明确记录未保存，尽力发送故障提示并停止接入，不宣称重投成功。

SDK在应用回调之前的解析、异步分派与平台确认存在应用不可见窗口；本机制不提供端到端永不丢消息保证。没有第二个长连接，也不靠抓取历史猜测缺失消息。

本文件描述本地候选协议；实际部署、真实平台送达与Windows监督安装须独立验收。


## 通用成果文件状态（schema23）

业务库新增 artifact_file_deliveries，保留每轮每路径稳定标识、文件名、文件身份、内容摘要、快照引用及逐件状态。capture_pending 为等待后台捕获，preparing 为捕获中，pending 为可发送，submitted 为可能已提交，binding 为已知平台消息但回复关联待完成，done 为完成。rejected 与 uncertain 不会被重启清零；绑定失败只重绑，不重发。notice_* 独立保存失败通知状态。首次启用的 meta.artifact_delivery_enabled_at 阻止历史回填。

快照在业务数据库父目录的 artifact-snapshots。后台以发现时文件身份核验捕获前后状态；改变或消失须明确失败。快照及传输均在独立工作线程，主业务循环不等待大文件内容读取或上传。无引用的已完成/拒绝副本回收；capture/pending/submitted/binding/uncertain 引用必须保留。业务库、guardian/transport.sqlite、artifact-snapshots 与 guardian/media-blobs 应在维护停止后作为同一备份与回滚闭包处理。

GuardianStore 写模式首次打开时以 additive CREATE TABLE IF NOT EXISTS 建立 file_uploads(kind,key,sha256,filename,file_key,created)，主键为前四列；读模式不建表。受控离线迁移可打开一次写模式并关闭，不启动消息服务。上传成功先保存 file_key，再创建消息；明确安全重试复用已保存 key，未知消息结果不自动重传。旧业务schema10/22由StateStore升级到23，不删除旧未决记录；同代回滚必须保留全部新状态与引用，不能只恢复旧业务库。

平台文件限制依据：[上传文件官方文档](https://open.feishu.cn/document/server-docs/im-v1/file/create.md)。平台 UUID 去重有时间窗口，本系统依赖持久状态防重复，不把 UUID 当无限期保证。artifact-status 提供只读分页结果，不执行重试。临时限流/未提交退避，明确永久拒绝结束该项，未知结果冻结并给出自然语言说明。


本机运维可执行 `artifact-status --json`，可选 `--thread-id`、`--offset`、`--limit` 分页过滤。结果包含技术状态、原因码和通知自身状态，查询不会重发。unknown 禁止通过删库、清零或原key盲重试处理；先核对平台结果与双库关联，按明确人工决策处理。


原生生成图使用同一独立持久队列的 media_kind=image，调用原生send_image展示，文件则为media_kind=file/send_file。Luna silent不会删除该队列。新事件交给此队列后不再另建旧图片outbox；若某项已存在于旧notification_media_deliveries，则仍由旧队列负责，不搬动旧unknown或重发旧消息。首次启用水位共同约束图片和文件。原生图片按10,000,000字节保守校验，超限提示为图片限制，不混同普通文件30MB。


捕获与发送交错执行，先发送已排队文件并回收，再逐件捕获/发送，避免同批16个大文件瞬间挤满配额。存储不足时保留capture_pending与30秒退避，并给出持久容量提示；后续仍按发现时身份校验，不能改发变化后的源文件。失败通知按明确原因生成稳定幂等键，同一原因不重复，容量等待后发现源文件变化可报告新的确定事实。


失败通知在提交前持久化notice_key；已提交、等待绑定或结果未知时，文件原因更新不能改动原通知身份。恢复成功会撤销尚未提交的过期容量提示。源文件已变化或缺失导致的确定拒绝不会因同轮晚到图片投影复活，避免发送后来替换的内容。
