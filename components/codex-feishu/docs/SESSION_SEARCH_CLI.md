# 会话搜索 CLI/JSON 契约

契约版本：`schema_version = 1`。该接口是飞书机器人与百宝箱共用的唯一权威会话搜索实现；调用方不得自行复制语义检索逻辑。

## 命令

```powershell
python progress-wx.py --config .\config.yaml session-search `
  --request-file D:\Temp\session-search-request.json `
  --progress-file D:\Temp\session-search-progress.json `
  --cancel-file D:\Temp\session-search.cancel `
  --cache-mode persistent `
  --json
```

`--request-file -` 表示从 stdin 读取。请求正文禁止放入命令行参数；请求最大 64 KiB，编码为 UTF-8 或 UTF-8 BOM。`--progress-file` 必填。`--cancel-file` 可选；搜索期间只要该路径存在，后端就会在安全检查点停止。

`--cache-mode` 固定为以下两种语义，省略时为 `persistent`：

- `persistent`：真实用户搜索。读取并更新生产状态库中的描述、证据和 Luna 判断缓存，供后续相同内容哈希复用。
- `ephemeral`：UI、DPI、联调和验收专用。后端先用 SQLite 在线备份把生产状态只读复制到系统临时目录，因此监测状态和已有缓存仍可读取；本次新增或失效的缓存只写临时副本，进程结束后自动删除，生产状态库不会被修改。百宝箱自动化验收必须使用此模式，不能再把默认模式称为“只读验收”。

成功时 stdout 只输出一个紧凑 JSON 对象和结尾换行。错误说明写入 stderr，stdout 保持为空。

## 请求 JSON

```json
{
  "schema_version": 1,
  "name": "可能记错的名称",
  "description": "这段会话大概做过什么",
  "last_activity": "这几天",
  "scope": "auto"
}
```

全部字段语义：

- `schema_version`：可选整数，省略时按 `1`；其他版本拒绝。
- `name`：可选字符串，最多 1000 字符。
- `description`：可选字符串，最多 1000 字符。
- `last_activity`：可选字符串，最多 1000 字符；自然时间会作为带容差的初始范围，无法解析时仍作为普通语义线索。
- `scope`：可选字符串，默认 `auto`；允许 `auto`、`hint`、`recent_30d`、`recent_180d`、`all`。`hint` 在没有可解析时间时安全回退为 `recent_30d`。

名称、描述和时间均可为空、记错或填错位置；所有非空文本作为共同语义线索。未知字段会拒绝，防止调用方误以为某字段已经生效。

## 原子进度 JSON

后端以“同目录临时文件 + 原子替换”写入进度文件，调用方可实时轮询：

```json
{"schema_version":1,"phase":"scoring","current":6,"total":9,"message":"【生产持久缓存】正在使用 Luna 判断少量候选"}
```

五个字段始终必填：

- `schema_version`：固定 `1`。
- `phase`：当前阶段；可见值包括 `starting`、`collecting`、`reading`、`narrowing`、`scoring`、`completed`、`cancelled`、`failed`。
- `current`：当前非负整数进度。
- `total`：当前阶段非负整数总量；尚未知时为 `0`。
- `message`：不含搜索正文的用户可读阶段说明，最多 300 字符；每个快照都以 `【生产持久缓存】` 或 `【隔离临时缓存】` 开头，UI 必须原样展示或提供等价的明确状态。

进度文件是状态快照，不是事件日志；轮询方应整文件读取，不要依赖每次中间阶段都能观察到。

## 最终 JSON

唯一结果样例：

```json
{
  "schema_version": 1,
  "search_id": "c4e2c27c9d4d4b41a3ab4a0f27635df1",
  "status": "found",
  "scope": "recent_30d",
  "scope_label": "最近30天",
  "can_expand": true,
  "next_scope": "recent_180d",
  "cost_warning": "扩大后预计检查 42 个会话，最多对 12 个候选调用 2 轮 Luna；通常约需 6～10 分钟，并会使用少量 Codex 每周额度。",
  "examined_count": 18,
  "semantic_candidate_count": 12,
  "model_call_count": 2,
  "matches": [
    {
      "thread_id": "01example",
      "title": "会话名称",
      "title_origin": "codex_generated",
      "description": "这段会话实际完成的工作说明。",
      "last_result": "复用进度监测同款逻辑生成的最后一轮结果。",
      "last_activity_at_beijing": "2026-08-28 12:30",
      "score": 0.93,
      "confidence": "high",
      "classification": "strong_match",
      "reason": "支持本次匹配的具体证据。",
      "project_id": "project-1",
      "project_name": "项目名称",
      "archived": false,
      "host_id": "local",
      "monitor": {"monitored": true, "origin": "manual", "expires_at": null}
    }
  ],
  "warnings": []
}
```

顶层字段全部必填：

- `schema_version`：固定整数 `1`；调用方遇到其他版本必须拒绝按本契约解释。
- `search_id`：本次调用生成的不透明唯一字符串，仅用于关联 UI 状态和诊断；调用方不得从其内容推断范围或结果。
- `status`：`found`、`ambiguous` 或 `not_found`。
- `scope`/`scope_label`：实际搜索范围及用户可读名称。
- `can_expand`：是否还能扩大。
- `next_scope`：下一级范围；没有时为 `null`。调用方必须把完整原请求保存下来，并在用户明确确认后将 `scope` 改成此值再次调用。
- `cost_warning`：下一级预计会话数、最多 Luna 批次、耗时和额度提示；已到全部范围时说明不能继续扩大。
- `examined_count`：当前范围内读取到已完成轮次的会话数。
- `semantic_candidate_count`：本次真正进入深层语义评分的候选数。普通范围先限制为本地前 12；`scope=all` 会按每批 6 项继续，直到全部候选完成，或当前唯一结果已通过剩余候选的理论最高分证明不可能被反超，因此 all 时可以大于 12。
- `model_call_count`：本次实际发起的 Luna 进程尝试数；缓存命中时可为 0。三项线索全空时仍按最后活动时间列出最近 3 个会话；只有缺少独立标题且尚无判断缓存的候选会进入一个普通 Luna 批次，以同时生成 description 和 display_title，不会另起标题专用调用。
- `matches`：按匹配度降序的数组；`not_found` 时为空。飞书/百宝箱每页最多展示 3 项，但不得丢弃后续候选。
- `warnings`：结构化读取警告数组。每项至少含字符串 `code`，涉及单会话时含字符串 `thread_id`，可含字符串数组 `details`；调用方必须向用户说明存在不完整记录，不能静默吞掉。`code` 是可扩展值，调用方不得因出现未知 code 而使整个搜索失败。

每个 match 字段全部必填：

- `thread_id`：Codex 完整会话 ID；传给打开会话、监测添加/移除等后续接口时必须原样保留。
- `title`：用于界面展示的会话名称，结果中始终为非空字符串。后端按人工 `name`、SQLite 当前自动 `title`、可核验为独立短标题的追加式 session_index 顺序取值；陈旧 session_index 不得覆盖较新的 SQLite 标题。只有 Codex 当时标题生成失败、两处都只剩首轮提示词回退的历史异常，才使用按内容版本持久保存的恢复性概括。正常搜索可复用同一轮 Luna 语义评分顺带生成的名称；旧历史也可由显式、有调用上限的维护命令一次性恢复。列表刷新不会调用模型，它也不会写回 Codex 会话元数据。
- `title_origin`：标题来源，固定为 `codex_manual`、`codex_generated`、`recovered_summary` 或 `unavailable`。`recovered_summary` 表示 SQLite、session_index 与 Desktop 当前均没有可用的 Codex 独立标题，`title` 只是恢复性内容概括；界面必须向用户明确说明，不能冒充原始标题。用户人工重命名或 Codex 后续补齐标题后，当前权威标题必须立即覆盖该概括。
- `description`：根据该会话真实历史生成或读取缓存的简短工作说明，不是用户本次搜索原文。
- `last_result`：复用进度监测当前同款摘要逻辑得到的最后一轮结果；不是整段历史原样转发。
- `last_activity_at_beijing`：`yyyy-MM-dd HH:mm` 字符串，来自产生最后结果的最后一个 `completed` 且确实包含最终答复或生成图片的轮次 `completedAt`。较新的失败、中断、空完成轮次和线程列表 `updatedAt` 都不能冒充该时间。
- `score`：`0.0`～`1.0` 的综合匹配分，最多四位小数；越高越匹配，只用于排序和展示，调用方不得自行改变后端 found/ambiguous 判定。
- `confidence`：后端根据综合分归一化的 `high`、`medium` 或 `low`。
- `classification`：Luna 的语义分类，固定为 `strong_match`、`possible_match` 或 `unlikely`。正常有线索搜索不会把 `unlikely` 放入 matches；调用方仍必须向前兼容该枚举。
- `reason`：支持或反对当前匹配的简短证据说明。
- `project_id`：所属 Codex 项目 ID；个人会话为空字符串。
- `project_name`：项目名称；个人会话固定为“个人会话”，项目名称不可读时可能回退为项目 ID。
- `archived`：布尔值，表示该会话是否已归档。
- `host_id`：Codex 主机 ID；本机记录通常为 `local`，调用方应把它当不透明字符串。
- `monitor`：固定对象，三个子字段全部必填。`monitored` 为布尔值；`origin` 在已监测时为 `manual` 或 `auto`，未监测时为 `null`；`expires_at` 为 Unix 秒整数或 `null`，手动长期监测和未监测通常为 `null`。

飞书呈现层不会修改以上 JSON 契约。多候选页每页最多显示 3 项，使用 `序号｜《title》`，候选之间保留空行；唯一命中和用户选择候选后的详情使用 `会话名称：《title》`，并复用“查询个人会话 → 选定 p01”的同一分块与富文本规则。`title_origin=recovered_summary` 时紧邻名称显示“Codex 当前没有可用的独立标题，当前名称为内容概括；在 Codex 中重命名后会自动更新”；正常标题不显示该提示。呈现层会先去掉 title 已有的一层中文书名号再统一包裹，避免重复；标题正文保持普通字重，只有字段名加粗，其他字段值、长正文、链接和命令文字也保持普通字重。书名号只存在于飞书正文，不写入 JSON、缓存键、选择编号或搜索评分。百宝箱只消费 JSON，不应依赖飞书正文排版。

语义判断缓存和独立的异常恢复名都按会话内容哈希保存；哈希包含标题来源。升级前的旧判断缓存没有展示名时会被当作未命中缓存，并在该会话下一次正常进入语义评分批次时一并重新判断。显式维护命令只处理已经确认没有独立标题的历史异常，每批最多 6 条、单次最多 3 批，并在 stdout 只返回计数，不返回私人标题正文。异步自动标题或人工重命名一旦落盘，来源/内容变化会使旧哈希自然失效；旧哈希记录不会在写入新版本时立即物理删除，而是保留到既有 90 天 `prune` 窗口，便于审计和回滚。精确哈希查询保证旧判断绝不会命中新内容。每次飞书展示仍重新读取当前 Codex 元数据，因此恢复性概括不会盖住后来出现的真实标题。

飞书持久搜索上下文另有内部标题格式版本，不属于本 CLI 契约。升级前保存的旧候选列表若无法从当前 Codex 元数据重新确认真实标题，机器人会明确要求重新发送“.搜索会话”，不会继续展示旧首轮提示词或把它伪装成名称。

多候选与未找到样例：

```json
{"schema_version":1,"search_id":"s2","status":"ambiguous","scope":"hint","scope_label":"最近5天（“这几天”）","can_expand":true,"next_scope":"recent_30d","cost_warning":"扩大后预计检查 25 个会话……","examined_count":6,"semantic_candidate_count":6,"model_call_count":1,"matches":[{"thread_id":"01a","title":"候选一","title_origin":"codex_generated","description":"说明一","last_result":"结果一","last_activity_at_beijing":"2026-08-27 10:00","score":0.78,"confidence":"high","classification":"strong_match","reason":"证据一","project_id":"","project_name":"个人会话","archived":false,"host_id":"local","monitor":{"monitored":false,"origin":null,"expires_at":null}},{"thread_id":"01b","title":"候选二","title_origin":"recovered_summary","description":"说明二","last_result":"结果二","last_activity_at_beijing":"2026-08-26 10:00","score":0.75,"confidence":"medium","classification":"possible_match","reason":"证据二","project_id":"","project_name":"个人会话","archived":false,"host_id":"local","monitor":{"monitored":false,"origin":null,"expires_at":null}}],"warnings":[]}
```

```json
{"schema_version":1,"search_id":"s3","status":"not_found","scope":"all","scope_label":"全部用户会话（含归档）","can_expand":false,"next_scope":null,"cost_warning":"已经检查全部用户会话，不能再扩大范围。","examined_count":76,"semantic_candidate_count":76,"model_call_count":13,"matches":[],"warnings":[]}
```

## 退出码、取消和文件生命周期

- `0`：成功产生最终 JSON；三种业务状态都属于成功。
- `2`：请求、配置、读取或模型错误；stderr 有说明，进度最终为 `failed`。
- `3`：调用方取消；stderr 有说明，进度最终为 `cancelled`。

调用方拥有 request、progress 和 cancel 路径：后端不会删除或覆盖 request，不会删除 cancel；progress 会持续原子覆盖并在结束后保留，供 UI 读取最终状态。调用方应为每次搜索使用独立目录或唯一文件名，并在进程结束、读完最终 JSON 后自行清理。后端为 Luna 创建的内部临时目录在每次尝试结束后自动删除，不留下搜索正文；`ephemeral` 的 SQLite 状态副本也在整个 CLI 调用结束时清理。即使请求无结果、取消或模型失败，隔离副本也不会合并回生产库。

Windows PowerShell 通过 stdin 调用时，CLI 直接读取 `stdin.buffer` 并按 UTF-8/BOM 解码，不受内嵌 Python 文本代码页影响；调用方仍必须实际写入 UTF-8 字节。使用 request 文件是 WinForms 最容易审计和取消的推荐方式。
