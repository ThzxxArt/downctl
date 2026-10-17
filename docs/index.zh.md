# downctl

一个基于插件架构的通用命令行文件下载器：断点续传、多连接加速、断线自愈。

**一切皆插件**——源、传输、校验、落地、进度、路由、钩子共用同一套接口，
内置功能也使用同一套接口实现。

- 简单：`downctl get <目标>` 一行开始。
- 可靠：中断自动续传，只补缺失分片，绝不静默损坏。
- 快速：单文件多连接并行，可多文件并发。
- 通用：HTTP(S)、HuggingFace、GitHub Releases、S3、SFTP、本地文件。
- 可扩展：任何人都能发布自己的源/传输/校验/钩子插件。

## 安装

```bash
pip install downctl            # 来自 PyPI
pipx install 'downctl[sftp]'   # 作为命令行工具并含 SFTP 支持
```

需要 Python 3.11+。Docker：

```bash
docker build -t downctl .
docker run --rm -v "$PWD:/data" downctl get <目标>
```

## 快速开始

```bash
# 普通文件
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# HuggingFace 仓库（仅 *.safetensors，2 个文件并发）
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# GitHub Release
downctl get gh://cli/cli --include '*.tar.gz'

# S3 前缀（使用 AWS 凭据）
downctl get s3://my-bucket/datasets/ --include '*.parquet'

# 只探测不下载
downctl probe https://example.com/big.iso

# 查看环境与插件
downctl doctor
downctl plugin list
```

中断了吗？**重跑相同的命令即从断点继续。**

## 下载源

| 源 | 目标写法 |
|---|---|
| URL | `https://…` |
| HuggingFace | `hf://org/name[@revision]`、裸 `org/name` |
| GitHub Releases | `gh://owner/repo[@tag]` |
| S3 | `s3://bucket/key`、`s3://bucket/prefix/` |
| SFTP | `sftp://[user@]host[:port]/path`（需 `downctl[sftp]`） |
| 本地文件 | `file:///path` |

## 配置

`~/.config/downctl/config.toml`（可用 `DOWNCTL_CONFIG` 覆盖），值支持
`${环境变量}` 插值。

```toml
[core]
dir = "~/Downloads"
jobs = 1
retries = 0
connections = 16
stall = 30
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["logfile", "archive", "webhook", "manifest"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"
# api = "https://api.github.com"

[plugins.s3]
# region = "us-east-1"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# session_token = "${AWS_SESSION_TOKEN}"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# path_style = true
# expires = 3600

[plugins.sftp]
# user = "me"
# password = "${SFTP_PASSWORD}"
# key = "~/.ssh/id_ed25519"
# key_passphrase = ""
# port = 22
# known_hosts = "~/.ssh/known_hosts"

[plugins.archive]
# into = "~/Downloads/extracted"
# keep = true

[plugins.webhook]
# url = "https://example.com/downctl"
# events = ["on_finish"]

[plugins.manifest]
# filename = ".downctl.manifest"

[plugins.tqdm]
enabled = true
```

### 常用环境变量

`DOWNCTL_CONFIG`、`HF_TOKEN`、`GITHUB_TOKEN`/`GH_TOKEN`、
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`、
`https_proxy`/`HTTP_PROXY`、`WSL_HOST_IP`、`NO_COLOR`、
`DOWNCTL_TRUST_PARTIAL`、`DOWNCTL_RETRY_BASE`。

## 目录

- [升级](upgrading.md)
- [命令行参考](cli.md)
- [下载源](sources.md)
- [配置](configuration.md)
- [钩子](hooks.md)
- [插件开发](plugins.md)
- [故障排查](troubleshooting.md)
- [设计文档](design.md)

## 许可证

MIT © ThzxxArt
