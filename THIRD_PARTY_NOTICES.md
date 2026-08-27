# Third-Party Notices

本仓库源码采用 MIT License；第三方组件保留各自许可证与版权。

- Python 3.13.14：Python Software Foundation License。Release 只使用 python.org 官方安装器，并验证 Authenticode 签名。
- PyYAML、websocket-client、lark-channel-sdk、requests、httpx、websockets、pycryptodome 等 Python 依赖：版本和 SHA-256 见 `components/codex-feishu/requirements-*.txt`。
- .NET 8：TreasureChest 发布为 Windows x64 self-contained single-file，相关 Microsoft 许可随运行时适用。
- 飞书/Lark SDK 与 API：使用者仍须遵守飞书开放平台条款。

发布构建必须保留 wheel 内许可证元数据，不得用未锁版本或未知来源二进制替换 Release 载荷。

