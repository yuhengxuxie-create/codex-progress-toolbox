# X 来源冷却与额度预警规则审计

本地候选，2026-09-09。不是解除 X 限制或覆盖全部帖文的承诺。此改动不接入付费 API、不更换出口、不扩大请求频率，没有模式分类模型或新定时任务。

## 来源状态

三个独立端点为 syndication（直接发现）、oembed（官方正文及作者核验）、x_parent（原页直接父关系核验）。原有 forecast 与 OpenAI 来源分别执行。

每次端点请求发起前保存实际尝试时间，响应后立即保存结果与冷却；既有 source cursor 原子更新，无数据库 schema 迁移。冷却跳过不发请求、不刷新尝试或成功时间、不累加429。若复合 X 来源本轮只有缓存或跳过，也不刷新来源尝试/成功时间。缓存可用于原有事件去重，不能称本次刚核验。

仅接受解析后的 Retry-After 非负整数秒或 HTTP 日期，以及 x-rate-limit-limit/remaining/reset 的非负整数。相同重复值接受，冲突重复值或非法形式忽略，绝不存原始头、Cookie、认证、错误正文。Retry-After 秒从收到响应算起；绝对日期和 reset 时间取未来值，多个有效等待取最晚。

无有效等待时，连续429本地退避为1/2/4/8/12小时，随后保持12小时；成功复位。有效服务器等待不被12小时上限截短。最多128位数字精确保存在JSON；更长的纯数字等待标记为不可表示并保持等待，不回退成较短本地期限。正常表示范围外的时间不会使状态CLI溢出。

实际请求仍在北京时间08–23的既有小时槽内进行。因此“最早允许”与“下次计划请求”分别表示，冷却到23:30则最早计划为次日08:00。全局 next_check_at 仅是来源扫描计划，不是每个端点的重试时间。

## 兼容 CLI 字段

`reset-alert-status` 的 schema_version 保持1；仅在 `source_states` 的 `x_thsottiaux` 项增加可选字段：

| 字段 | 语义 |
|---|---|
| x_endpoint_states.syndication/oembed/x_parent | 端点独立状态对象 |
| state | cooldown、retry_due、available、error或never；到期不等于成功 |
| retry_not_before / next_attempt_at | 最早允许与计划小时槽，带+08:00的ISO时间或null |
| last_attempt_at / last_success_at | 真实请求/验证时间，ISO或null；跳过不更新 |
| consecutive_429 / cooldown_basis | 连续429次数；server/local_backoff/空字符串 |
| retry_unrepresentable | 等待时间不可表示；此时不能根据null猜测允许重试 |
| last_error_code | 固定错误代码或null |
| fallback_discovery | forecast的实际state available/unavailable/unknown、candidate_count及checked_at |
| official_verification | state verified/cached/unavailable/not_needed/unknown、attempted、verified_count、live_verified_count、last_error_code |

verified_count包含本轮可用缓存与父上下文；live_verified_count仅本轮实际核验成功的子帖。cached不会宣称本次网络核验成功。缺字段意味着旧后端或未知，不能假定正常。coverage仍保留degraded，备用可用不能证明直接发现完整。

## 原始要求与现行授权矩阵

| 要求 | 核验结论与代码/测试证据 |
|---|---|
| 北京时间08–23整点 | 已有 schedule_slot/next_check_at 和 test_schedule_uses_fixed_beijing_hours；进程晚启动或中断可在该小时补跑，非硬实时整点保证 |
| 夜间不运行 | 来源扫描已有门禁；审计发现旧出站缺口后获批修复deliver_one入口，00–07:59不领取/提交/改待发队列，08先过期后投递；23:59已提交的外部请求可跨午夜返回并记结果，不虚称可撤回 |
| 08补查00–08、优先新增 | 24h回看覆盖00–08，候选按新到旧排序并持久去重；这是后续补漏修复策略，并非严格只读取自上次新增。test_0800_window_is_midnight_then_uses_source_cursor、test_empty_x_baseline_rechecks_midnight_and_recovers_live_a_once |
| X新帖和公开回复 | 解析独立发现及备用实际提供的候选，官方oEmbed核作者/正文，原页确认直接父链；覆盖限制：不能证明来源收录全部公开回复，429冷却不会新增覆盖 |
| 预测站变化 | 只把第三方当候选/预测，单独高概率不预警；来源错误不伪造预测成功，官方正文仍独立核验 |
| OpenAI Status Codex及其它可信官方额度 | 已接Status与固定官方额度文档，文档结构/正文变化核验；未覆盖所有可能官方渠道，不能声称全网覆盖 |
| A明确未来24h额度承诺 | 新修订要求明确未来承诺及精确未来时间，排除疑问、推测、否定；原test_classifier_a_requires_explicit_quota_object_and_exact_24h_time与新test_noncommitment_never_becomes_a_or_b |
| A同帖或直接上下文对象无歧义 | 保留核验父链供给对象，未证关系不能借父帖升级；test_verified_reply_context_can_supply_quota_object_for_a_or_b2。明确支持子帖自身承诺如We will reset them；仅Yes, in 2 hours即使父问已核，当前保守不报，test_verified_yes_only_reply_is_explicitly_unsupported，不宣称支持所有无歧义短答 |
| 裸reset、玩笑、发布不能A | 原排除继续保留；新B1同样排除玩笑/过去，不用高预测重新激活 |
| B1预测≥70且独立官方信号 | 保留独立发现X不在forecast evidence_ids、Status或实际变化官方文档；FAQ标题疑问不排除另一事实句；test_b1_requires_independent_official_quota_signal |
| B2未来某日额度动作且时间/范围歧义 | 需额度对象或已核直接上下文与明确动作；today/tomorrow/weekday等不虚构精确执行时刻，给B。真实banked reset资格条件仍保留 |
| B3官方故障加补偿意向、时间未明 | 新修复官方X与Status均可；will承诺或working on compensation/make this right with quota credits等意向均可B；纯服务恢复不行 |
| 官方承诺优先于低预测并标冲突 | 原A优先保持，新修复在单句证据标注新鲜低预测冲突，不压掉A、不增加独立重复事件 |
| 过去时间不误成新未来 | 新红线覆盖过去ISO再带today、过去日期再带weekday、last weekday；不能回退成今天/下周 |
| 已发生与后续授权 | 原未来预警排除已发生；后续明确授权的announced_available（已重置/券发放）继续通知，注明未核用户个人到账，不删除此扩展 |
| 无A/B静默但有记录 | _record_results保存来源/信号，只有classify决策进入reserve_reset_alert_event；没有决策不产生预警。正常模块心跳不等于预警 |
| 去重、过期、未知发送 | 原fingerprint/event_key、过期与持久unknown冻结继续；旧事件和队列不重置、不重放 |
| 最多4行 | reserve_reset_alert_event强制恰4行：级别、证据、窗口、建议；test_alert_message_is_exactly_four_lines_and_latest_has_event_id |
| 建议内容 | B继续观察；A按后续券资格说明建议查看官方公告/规划使用，不保证个人到账或强制消耗 |

## 验证边界

所有新红线使用合成HTTP/SQLite/时钟/渠道，没有向X强刷或真实发送预警。确定性语言判据不是任意自然语言的完整理解器；没有证据的条件保持不告警，不能承诺零漏报。当前观察到的公开来源覆盖、客户端冷却行为、规则判级与真实平台送达是不同证据，不互相替代。
