# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![Docs](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml/badge.svg)](https://thzxxart.github.io/downctl/)
[![coverage](https://thzxxart.github.io/downctl/coverage.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | **简体中文**

> 一个基于插件架构的通用命令行文件下载器：断点续传、多连接加速、断线自愈，一切皆插件。

`downctl` 可以从几乎任何地方可靠地下载文件。可续传的**分片位图**、**多连接加速**、
**卡死检测**与**自动重试**是稳定内核的一部分；其余能力——下载源、传输、校验、落地、
进度呈现、路由与生命周期钩子——全部是插件，而且**内置功能使用与第三方完全相同的插件接口**。

- 简单：`downctl get <目标>`，一行命令开始下载。
- 可靠：中断/断线后**自动续传**，只补缺失的分片，绝不从头重下、绝不静默损坏。
- 快速：单文件多连接并行，可多文件并发。
- 通用：HTTP(S) / HuggingFace / GitHub Releases / S3 / SFTP（需 `downctl[sftp]`）/ 本地文件开箱即用。
- 可扩展：一切皆插件，任何人都能发布自己的源、传输、校验器或钩子。

> 状态：**pre-1.0**。下载内核与 CLI 已端到端可用，详见[设计文档](docs/design.md)。

---

## 目录

- [特性](#特性)
- [安装](#安装)
- [升级](#升级)
- [快速开始](#快速开始)
- [使用详解](#使用详解)
- [内置下载源](#内置下载源)
- [子命令参考](#子命令参考)
- [配置文件](#配置文件)
- [插件系统](#插件系统)
- [常见问题 / 故障排查](#常见问题--故障排查)
- [开发](#开发)
- [文档与许可](#文档与许可)

---

## 特性

- **断点续传**：以分片位图（`.downctl-pieces`）作为权威账本。多连接下载分片乱序完成是常态，
  位图精确记录哪些分片已完成，重试时**只补缺失分片**；中断、断电、`Ctrl+C`、`kill -9`
  之后再运行同一条命令，即从断点继续。
- **防静默损坏**：中断残留的稀疏文件、中间有空洞的文件不会被误判为“已完成”，
  必要时重下，保证最终文件内容正确。
- **多连接加速**：单文件默认 16 连接并行，分片粒度约 8 MB（小文件自动变小），
  每次中断最多损失「连接数 × 8 MB」的进度。
- **断线自愈**：网络闪断、代理抖动、服务端卡死都会被“卡死看门狗”捕获并自动重连续传，
  直至文件完整，无需人工干预。
- **多文件并发**：`-j/--jobs` 并发下载多个文件。
- **校验**：支持单个 `--checksum sha256:<hex>`，以及 `--checksums`（`sha256sum` 风格清单）。
- **镜像 / 代理**：`--mirror FROM=TO` 前缀改写；代理支持参数与环境变量，WSL 下自动探测宿主代理。
- **内置源**：普通 URL、HuggingFace 仓库整仓、GitHub Releases、S3、SFTP。
- **一切皆插件**：7 类插件（源 / 传输 / 校验 / 落地 / 进度 / 路由 / 钩子），内置即插件。
- **跨平台**：Windows / macOS / Linux / WSL 全兼容，自动处理控制台颜色与编码。

---

## 安装

### 环境要求

- **Python 3.11 或更高版本**（依赖 `asyncio.TaskGroup`、异常组等特性）。
- 运行时依赖：`aiohttp`、`tqdm`（pip 会自动安装）。
- 可选依赖：`asyncssh`（仅在使用 SFTP 源时需要）。

### 方式一：pip（最通用）

```bash
python -m pip install downctl
```

若系统 Python 受 PEP 668 保护（Ubuntu 24.04+ / Debian 12+），可改用：

```bash
python -m pip install --user downctl
# 或使用虚拟环境 / pipx（见下）
```

### 方式二：pipx（推荐用于命令行工具）

`pipx` 会把 `downctl` 装进独立环境并加入 PATH，最适合全局命令：

```bash
pipx install downctl
# 需要 SFTP 支持时：
pipx install 'downctl[sftp]'
```

### 方式三：从源码安装

```bash
git clone https://github.com/ThzxxArt/downctl
cd downctl

# 常规安装
pip install .

# 可编辑安装（开发用）
pip install -e '.[dev,sftp]'
```

### 可选依赖

| 场景 | 安装命令 |
|---|---|
| 使用 SFTP 源 | `pip install 'downctl[sftp]'` |
| 开发（含测试/类型检查） | `pip install -e '.[dev,sftp]'` |
| 构建文档 | `pip install -e '.[docs]'` |

### 方式四：Docker

```bash
# 构建镜像
docker build -t downctl .

# 运行：把当前目录挂载到容器内 /data，下载的文件会出现在本地
docker run --rm -v "$PWD:/data" downctl get https://example.com/big.iso
```

### 验证安装

```bash
downctl version      # 打印版本
downctl doctor       # 打印环境、依赖、代理与已加载插件
```

---

## 升级

`downctl` 用标准方式安装/升级，无需卸载重装。

### pip

```bash
# 升级到最新版
python -m pip install --upgrade downctl

# 升级到指定版本
python -m pip install 'downctl==0.1.1'

# 升级并包含可选依赖（如 SFTP）
python -m pip install --upgrade 'downctl[sftp]'
```

### pipx

```bash
pipx upgrade downctl                 # 升级
pipx upgrade --include-injected downctl   # 连同注入的依赖
pipx install --force 'downctl[sftp]' # 需要加装可选依赖时
```

### 验证与排障

```bash
downctl version        # 确认版本号已更新
downctl doctor         # 确认依赖/插件/代理正常
```

### 升级注意

- 升级**不会**动你的下载文件、`~/.config/downctl/config.toml` 及残留位图/失败清单。
- 新增/变更的配置键向后兼容；未知键会被忽略（`config show` 可查看生效值）。
- 0.1.1 起版本号来自包内 `__version__`；用 `downctl version` 或 `pip show downctl` 核对。
- 若从源码安装：`git pull && pip install -U .`（或 `pip install -U -e '.[dev,sftp]'`）。
- Docker：重新 `docker build -t downctl .` 即可。

---

## 快速开始

```bash
# 1) 下载一个普通文件到指定目录，限速 5 MB/s
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# 2) 下载 HuggingFace 仓库中所有 safetensors 权重（2 个文件并发）
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# 3) 只探测大小和状态，不实际下载
downctl probe https://example.com/big.iso

# 4) 查看插件与环境
downctl plugin list
downctl doctor

# 5) 查看某个目录里未完成 / 失败的下载
downctl status ~/Downloads
```

中断了吗？**再运行一次同样的命令即可续传。**

---

## 使用详解

### 基本下载

```bash
downctl get <目标> [更多目标...] [选项]
```

`<目标>` 可以是：

- 普通 `http(s)://` 链接；
- `hf://org/name[@revision]` 或裸 `org/name`（HuggingFace 仓库）；
- `gh://owner/repo[@tag]`（GitHub Releases）；
- `s3://bucket/key` 或 `s3://bucket/prefix/`（S3 对象/前缀）；
- `sftp://[user@]host[:port]/path`（SFTP，需可选依赖 `asyncssh`）；
- `file:///本地/路径`（本地文件或目录）。

可一次传入多个目标，顺序或并发下载。

### 保存位置

```bash
downctl get <url> -d ~/models        # -d/--dir：保存到目录（默认 ~/Downloads）
downctl get <url> -o ~/a/renamed.bin # -o/--output：单文件另存为指定完整路径
```

### 并发与连接数

```bash
downctl get <url> -j 4          # 并发下载 4 个文件
downctl get <url> --connections 32   # 单文件使用 32 个连接（默认 16）
```

### 限速

```bash
downctl get <url> -t 5M     # 5 MB/s
downctl get <url> -t 512k   # 512 KB/s
```

限速是**全局**的：`-t` 的预算是所有并发文件共享的总带宽。

### 重试

```bash
downctl get <url> -r 3      # 每个文件最多重试 3 次
downctl get <url> -r 0      # 0 = 无限重试（默认，直到成功）
```

### 断点续传与中断恢复

`downctl` 默认就支持断点续传，**不需要额外开关**：

- 下载过程中会在目标文件旁写一个位图 `<文件名>.downctl-pieces`，记录每个分片的状态；
- 中断（`Ctrl+C`、网络断开、机器重启）后，重新运行**完全相同的命令**即可从断点继续，
  只会补下载缺失的分片；
- 完成后位图自动删除；
- 用 `downctl status <目录>` 可以查看哪些文件未完成、进度百分比与失败清单。

卡死看门狗：若连续 `--stall`（默认 30）秒无任何进展，会自动断开并重连续传。

### 校验

```bash
# 单文件：下载后校验 sha256，不匹配自动重下（超限后放弃）
downctl get <url> --checksum sha256:<64位十六进制>

# 批量：按 sha256sum 风格清单校验（每行 "<hex>  <文件名>"）
downctl get <url> --checksums ./SHA256SUMS
```

`manifest` 钩子可以自动为已完成的下载生成这样一份清单（见 [钩子](#内置钩子)）。

### 代理

```bash
downctl get <url> -p http://127.0.0.1:7897
```

也会自动读取环境变量 `https_proxy` / `HTTPS_PROXY` / `http_proxy` / `HTTP_PROXY`。
在 WSL 下若未显式指定代理，会自动探测 Windows 宿主 IP 并启用宿主代理。

### 镜像

```bash
# 把主站前缀改写为镜像前缀（例如 HuggingFace 用 hf-mirror）
downctl get <url> --mirror https://huggingface.co=https://hf-mirror.com
```

### 从文件读取目标清单

```bash
downctl get -l ./targets.txt
# 或从标准输入读取（管道）
cat targets.txt | downctl get -l -
```

### 失败重试与容错

```bash
downctl get <url1> <url2> --continue       # 单个目标解析/探测失败时跳过，继续其余的
downctl get --retry-failed                 # 重试上次失败清单里的目标（保留子路径与校验）
```

失败清单保存在输出目录的 `.downctl.failed`。第 2 条命令会读取它并**原位重下**——
包括 HuggingFace / GitHub 仓库里的子目录路径与预期校验和。

### 干跑 / 静默 / 详细

```bash
downctl get <url> --dry-run     # 只探测大小与状态，不下载
downctl -q get <url>            # 静默：只输出错误，并切换到 quiet 进度
downctl -v get <url>            # 详细：额外的诊断信息
```

### 交互向导

```bash
downctl wizard    # 交互输入目标与目录后开始下载
```

### 自定义 User-Agent

```bash
downctl get <url> --user-agent 'my-bot/1.0'
```

### 自定义请求头与 Basic 认证

```bash
downctl get <url> -H 'X-Api-Key: xxx' -H 'Accept: application/json'
downctl get <url> -u user:password
```

### Shell 补全（可选）

```bash
pip install 'downctl[completion]'
register-python-argcomplete downctl >> ~/.bashrc   # bash
# zsh: register-python-argcomplete --shell zsh downctl >> ~/.zshrc
```

---

## 内置下载源

### 普通 URL（`url`）

```bash
downctl get https://example.com/file.bin
```

### HuggingFace（`huggingface`）

```bash
downctl get hf://meta-llama/Llama-3-8B                     # 整仓
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors'
downctl get hf://datasets/squad                            # 数据集
downctl get hf://org/repo@v1.0                             # 指定 revision
downctl get org/repo                                       # 裸 repo id 亦可
```

- 通过 Hub API 展开文件清单，自动使用仓库的 **LFS sha256** 做下载后校验；
- 支持 `--include/--exclude` 通配、`--revision`；
- 配置 `[plugins.huggingface] mirror` 可走镜像；`token` 或环境变量 `HF_TOKEN`
  用于私有仓库 / 提高限额。

### GitHub Releases（`github`）

```bash
downctl get gh://cli/cli                        # 最新 release 的全部 assets
downctl get gh://cli/cli --include '*.tar.gz'   # 只下匹配项
downctl get gh://owner/repo@v1.2.3              # 指定 tag
```

- 通过 REST API 列出 assets，使用其 `sha256` 摘要（若提供）做校验；
- 配置 `[plugins.github] token`（或环境变量 `GITHUB_TOKEN`）以避免匿名接口限流；
  私有仓库需 token（会随下载请求一并携带）；`api` 可指向 GitHub Enterprise。

### S3（`s3`）

```bash
downctl get s3://my-bucket/path/to/file.bin
downctl get s3://my-bucket/datasets/ --include '*.parquet'
```

- 采用 **SigV4 查询串预签名**（纯标准库），因此对象通过普通 HTTP 传输，
  支持 Range、多连接与续传；
- 前缀会以 ListObjectsV2 分页列举；
- 凭据来自 `[plugins.s3]` 或环境变量 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
  `AWS_SESSION_TOKEN`；`endpoint` / `region` / `path_style` / `expires`（预签名有效期秒数，≤7 天）
  可配以兼容 MinIO 等。

### SFTP（`sftp`，需可选依赖）

```bash
pip install 'downctl[sftp]'
downctl get sftp://user@host:22/home/user/data.bin
downctl get sftp://user@host/data/ --include '*.csv'
```

- 目录会列出其中的文件；
- SFTP 传输**支持 Range**（按偏移 seek），因此同样支持多连接与续传；
- URL 中的密码会被剥离，不会写入任务或失败清单；更推荐用
  `[plugins.sftp] password/key/known_hosts` 配置（另支持 `port`、`key_passphrase`）。
- **主机密钥默认校验**：存在 `~/.ssh/known_hosts` 时按其校验并拒绝未知主机；
  可用 `known_hosts = ""`（或 `none`/`false`）显式关闭校验。

### 本地文件（`file`）

```bash
downctl get file:///data/models/        # 复制整个目录（递归）
downctl get file:///data/a.bin -d /backup
```

- 目录会递归遍历，支持 `--include/--exclude`；
- 通过本地 `file` 传输读取（支持 Range），与其它源共用同一套流程。

---

## 子命令参考

| 命令 | 说明 |
|---|---|
| `downctl get <目标...>` | 下载目标（见上） |
| `downctl probe <目标...> [-p PROXY] [--json]` | 只探测大小 / HTTP 状态，不下载 |
| `downctl status [目录]` | 显示未完成（位图）与失败清单 |
| `downctl clean [目录] [--dry-run]` | 清理残留位图 / 失败清单 / 锁目录 |
| `downctl plugin list` | 按类别列出已发现插件 |
| `downctl plugin info <名称>` | 显示某个插件的类别与优先级 |
| `downctl config path` | 打印配置文件路径 |
| `downctl config init [--force]` | 写入默认配置文件 |
| `downctl config show` | 打印生效配置 |
| `downctl doctor` | 打印版本 / 依赖 / 平台 / 代理 / 插件 |
| `downctl wizard` | 交互式引导 |
| `downctl completions bash\|zsh\|fish` | 输出 shell 补全脚本 |
| `downctl version` | 打印版本 |

### get 的全部选项

| 选项 | 说明 |
|---|---|
| `-d, --dir DIR` | 保存目录（默认 `~/Downloads`） |
| `-o, --output PATH` | 单文件另存为指定完整路径 |
| `-j, --jobs N` | 并发文件数 |
| `--connections N` | 单文件连接数（默认 16） |
| `--stall N` | 卡死看门狗阈值秒数（默认 30） |
| `--piece-size SIZE` | 分片大小，如 `8M` / `4M`（默认自动） |
| `--min-split-size SIZE` | 小于该大小的文件走单连接 |
| `-t, --limit-rate RATE` | 限速，如 `5M` / `1m` / `512k` |
| `-r, --retries N` | 每个文件最大重试次数（`0`=无限） |
| `--force` | 忽略已有文件/续传状态，从头重下 |
| `--skip-existing` | 已存在（且大小匹配）的文件跳过 |
| `--checksum ALGO:HEX` | 下载后校验 |
| `--checksums FILE` | 按 `sha256sum` 风格文件校验 |
| `-p, --proxy URL` | 代理 |
| `-H, --header K:V` | 附加请求头（可多次） |
| `-u, --user USER:PASS` | HTTP Basic 认证 |
| `--mirror FROM=TO` | URL 前缀改写 |
| `--source NAME` | 强制指定源插件 |
| `--reporter NAME` | 进度呈现（`tqdm`/`json`/`quiet`） |
| `--include PAT` / `--exclude PAT` | 仓库/前缀源的过滤通配（可多次） |
| `--revision REV` | 仓库 revision |
| `--dry-run` | 只探测不下载 |
| `--retry-failed` | 重试失败清单 |
| `--continue` | 目标解析/探测失败时跳过而非中断整批 |
| `-l, --list FILE` | 从文件读取目标清单 |
| `--hook NAME` | 本次运行启用某钩子（可多次） |
| `--user-agent UA` | 自定义 HTTP User-Agent |
| `--local-plugins` | 同时加载本地插件目录（不受信任） |

全局选项：`-q/--quiet`、`-v/--verbose`（`-V/--version` 仅顶层，等价于 `downctl version`）。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 有一个或多个目标失败 |
| `130` | 被中断（进度已保留，重跑即续传） |

---

## 配置文件

默认路径：`~/.config/downctl/config.toml`（可用环境变量 `DOWNCTL_CONFIG` 覆盖）。
值支持 `${环境变量}` 插值。命令行参数优先于配置文件。

```toml
[core]
dir = "~/Downloads"     # 默认保存目录
jobs = 2                # 并发文件数（默认 1，此处仅为示例）
retries = 0             # 0 = 无限重试
connections = 16        # 单文件连接数
stall = 30              # 卡死看门狗秒数
# piece_size = 8388608  # 分片字节数（默认自动）
# min_split_size = 1048576  # 小于此值走单连接
# rate = 5242880        # 限速（字节/秒）
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["logfile", "manifest"]   # 每次运行启用的钩子

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"

[plugins.s3]
# region = "us-east-1"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# session_token = "${AWS_SESSION_TOKEN}"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# path_style = true
# expires = 3600          # 预签名有效期（秒，≤7 天）

[plugins.sftp]
# user = "me"
# password = "${SFTP_PASSWORD}"
# key = "~/.ssh/id_ed25519"
# key_passphrase = ""
# port = 22
# 默认用 ~/.ssh/known_hosts 校验主机密钥；显式置空可关闭（"", none, false）
# known_hosts = "~/.ssh/known_hosts"

[plugins.tqdm]
enabled = true
```

生成默认文件：

```bash
downctl config init
downctl config show
```

### 常用环境变量

| 变量 | 作用 |
|---|---|
| `DOWNCTL_CONFIG` | 指定配置文件路径 |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | HuggingFace 令牌 |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API 令牌 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 凭据 |
| `https_proxy` / `HTTP_PROXY` 等 | 代理 |
| `WSL_HOST_IP` | WSL 下手工指定宿主 IP |
| `NO_COLOR` | 关闭彩色输出 |
| `DOWNCTL_TRUST_PARTIAL` | 置 `1` 时，对“大小已达预期但无法探测空洞（如 WSL DrvFS）”的本地文件按完整处理 |
| `DOWNCTL_RETRY_BASE` | 重试退避基数秒数（默认 5，测试/调优用） |

---

## 插件系统

**一切皆插件。** 内核（调度、分片位图、看门狗、限速、锁、磁盘预检）是稳定 API；
其余全部走统一接口，内置功能也不例外。

| 类型 | 说明 | 内置实现 |
|---|---|---|
| Source | 把输入解析成下载任务 | `url`、`huggingface`、`github`、`s3`、`sftp`、`file` |
| Transport | 实际传输数据 | `http`、`sftp`、`file` |
| Verifier | 下载后校验 | `sha256` |
| Sink | 数据落地 | `filesystem` |
| Reporter | 进度与日志 | `tqdm`、`json`、`quiet` |
| Router | URL 改写 | `mirror` |
| Hook | 生命周期事件 | `logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum` |

**发现机制**（后者可按名称覆盖前者）：

1. 内置 `downctl.builtin`；
2. 已安装包通过 `downctl.*` entry-points 声明；
3. 本地目录 `./downctl_plugins` 与 `~/.config/downctl/plugins`（需 `--local-plugins`）。

### 编写插件

最小示例见 [`examples/plugins/`](examples/plugins)。插件是任意对象，只需暴露
`name` / `api_version` / `priority` 以及某个契约的方法（见
[`downctl.plugin.protocols`](src/downctl/plugin/protocols.py)）：

```python
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION


class ExampleSource:
    name = "example"
    api_version = API_VERSION  # "1"
    priority = 0

    def can_handle(self, target: str) -> bool:
        return target.startswith("example://")

    async def resolve(self, target: str, ctx) -> list[DownloadTask]:
        name = target[len("example://"):]
        return [DownloadTask(url=f"https://example.com/{name}", target=name, source=self.name)]
```

打包为可安装插件：

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

> **安全提示**：插件即任意代码执行。本地目录插件必须显式启用且只信任可信目录；
> entry-points 插件会自动加载，安装第三方插件等同于运行其代码。

### 内置钩子

钩子是**可选**的：用 `--hook NAME`（单次）或配置 `[core] hooks = [...]`（常驻）启用，
其选项来自 `[plugins.<name>]`。

- **`logfile`**：把生命周期事件追加写入文件。
  `path = "downctl.log"`。
- **`archive`**：下载完成后自动解压 `.zip` / `.tar(.gz/.bz2/.xz)`，
  并做**路径穿越与软/硬链接防护**。
  `into = "目录"`（默认同名兄弟目录）、`keep = true/false`。
- **`webhook`**：把选定事件以 JSON POST 出去（fire-and-forget）。
  `url`、`events`（默认 `["on_finish"]`）、`timeout`。
- **`manifest`**：为已完成的下载写一份 `sha256sum` 清单，可日后配合 `--checksums` 复验。
  `filename`（默认 `.downctl.manifest`）。
- **`exec`**：事件触发时执行 shell 命令，事件信息通过 `DOWNCTL_EVENT`/`DOWNCTL_TARGET`/
  `DOWNCTL_PATH`/`DOWNCTL_URL`/`DOWNCTL_OK` 环境变量传入。
  `command`（必填）、`events`（默认 `["on_task_finish"]`）、`timeout`（默认 300）。
- **`checksum`**：为每个已完成的下载写一份 `<文件>.<算法>` 边车（`sha256sum` 行格式）。
  `algo`（默认 `sha256`）、`events`（默认 `["on_task_finish"]`）。

例：

```toml
[core]
hooks = ["archive", "manifest"]

[plugins.archive]
keep = false

[plugins.manifest]
filename = ".downctl.manifest"
```

---

## 常见问题 / 故障排查

**下载中断了，会从头开始吗？**
不会。默认按分片位图续传，重跑相同命令只补缺失分片。
（`DOWNCTL_TRUST_PARTIAL=1` 仅影响“大小已达预期、但无法探测稀疏空洞”的本地文件判定，
不是用来续传比预期更小的部分文件的。）

**重复运行会不会重新下载？**
已完整的文件会被跳过。

**提示磁盘空间不足？**
下载前会做磁盘预检（保留 200 MB 余量）；空间不足时只失败当前文件，不影响其它文件。

**HuggingFace / GitHub 报 401/403/404？**
多为私有资源或匿名限流：配置相应 `token` 或用对应环境变量；也可用 `--mirror` 走镜像。

**代理相关？**
用 `-p` 或环境变量；WSL 下会自动探测宿主代理，必要时用 `WSL_HOST_IP` 手工指定。
`downctl doctor` 会显示探测到的代理。

**卡在某个文件不动？**
`--stall` 秒数内无进展会自动断开重连；若服务端长期不稳定，可调大或调小该值。

**如何查看还有哪些没下完？**
`downctl status <目录>`；失败项可 `downctl get --retry-failed` 重试。

**日志里有奇怪字符/乱码？**
输出流会自动转为 UTF-8；非终端环境自动关闭颜色（也可设 `NO_COLOR`）。

---

## 开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,sftp]'

ruff check .          # 代码风格
ruff format --check . # 格式
mypy                  # 类型检查（strict）
pytest                # 测试
pytest --cov=downctl --cov-fail-under=85   # 测试 + 覆盖率门槛（CI 用）
python -m build       # 构建 sdist / wheel
```

---

## 文档与许可

- [命令行参考](docs/cli.md)
- [下载源](docs/sources.md)
- [配置](docs/configuration.md)
- [钩子](docs/hooks.md)
- [插件开发](docs/plugins.md)
- [故障排查](docs/troubleshooting.md)
- [实战配方](docs/cookbook.md)
- [设计文档](docs/design.md)
- [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [发布流程](RELEASING.md)

本项目基于 [MIT 许可证](LICENSE) 发布，© ThzxxArt。
