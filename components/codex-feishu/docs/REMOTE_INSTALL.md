# 异地与离线安装

普通用户应使用 v1.5.0 full Release，而不是复制某台电脑的运行目录。

full 包包含：公共源码、TreasureChest 自包含程序、Python 3.13.14 官方安装器、哈希锁定 wheel、安装器和校验清单。安装前先运行根目录 `installer\verify-package.ps1`；Python 安装器必须具有有效的 Python Software Foundation 数字签名。

默认安装位置是 `%LOCALAPPDATA%\CodexFeishuEcosystem`，也可通过 `-InstallRoot` 选择其他本机非系统目录。所有脚本以解压目录和安装目录为基准，不依赖发布者电脑上的绝对路径。

从 v1.x 升级必须使用专用 upgrade 包。旧 Webhook、`config.local.json` 与新版企业应用凭据不兼容；升级器只迁移可验证的任务 ID，其他旧配置完整备份。旧完整生态的 `config.yaml`、`.secrets`、`.state`、数据库和 TreasureChest `config.json` 会保留，不用模板覆盖。

不要从第三方来源补齐缺失的 Python 安装器或 wheel。缺少文件或 SHA-256 不一致时，停止安装并重新下载官方 Release。
