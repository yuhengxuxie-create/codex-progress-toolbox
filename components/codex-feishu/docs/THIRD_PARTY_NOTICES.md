# 第三方组件说明

运行时依赖以 `requirements-core.txt` 和 `requirements-feishu.txt` 的固定版本与 SHA-256 为准，包括 PyYAML、websocket-client、lark-channel-sdk、requests/httpx 及其传递依赖。

Release 构建器只能从锁定清单下载 wheel，安装时必须使用 `pip --require-hashes --no-index`。Python 3.13.14 安装器来自 Python Software Foundation，执行前验证 Authenticode 签名。

各依赖的许可证和版权仍归其权利人。发布者应随二进制附件保留依赖许可证元数据和本文件，不得把本表当作替换依赖版本的授权。

