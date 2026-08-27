# 架构

```text
Codex notify / Desktop task tools / SQLite read-only state
                         │
                         ▼
        codex-feishu secure backend
        ├─ Codex management and approvals
        ├─ progress state, outbox, retry, dedupe
        └─ monitor-* CLI (schema_version=1)
             │                    │
             ▼                    ▼
 Feishu enterprise app       TreasureChest
 WebSocket + official API    desktop UI / service manager
```

Codex 管理与进度监测共享后台进程与安全状态，但逻辑边界清晰。TreasureChest 只调用稳定 CLI/JSON，不直接改 YAML 或 SQLite。飞书入站事件先做 open_id、p2p、message_id、引用和幂等校验，再进入业务队列。

产品版本与协议版本独立：生态、后台、百宝箱产品版本为 1.5.0；状态库和 CLI 的 `schema_version` 保持各自兼容规则。

更详细的后台设计见 `components\codex-feishu\docs\ARCHITECTURE.md`。
