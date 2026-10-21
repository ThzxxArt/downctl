# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![Docs](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml/badge.svg)](https://thzxxart.github.io/downctl/)
[![coverage](https://thzxxart.github.io/downctl/coverage.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ThzxxArt/downctl/blob/master/LICENSE)

[English](https://github.com/ThzxxArt/downctl/blob/master/README.en.md) | **简体中文**

> 一个基于插件架构的通用命令行文件下载器：断点续传、多连接加速、断线自愈，一切皆插件。

`downctl` 可以从几乎任何地方可靠地下载文件。可续传的**分片位图**、**多连接加速**、
**卡死检测**与**自动重试**是稳定内核的一部分；其余能力——下载源、传输、校验、
进度呈现、路由与生命周期钩子——全部是插件，而且**内置功能使用与第三方完全相同的插件接口**。

- 简单：`downctl get <目标>`，一行命令开始下载。
- 可靠：中断/断线后**自动续传**，可续传时只补缺失的分片、不从头重下、不静默损坏——
  前提是服务器提供了可用的**版本见证**（`ETag`/`Last-Modified`）或校验和；两者都没有时，
  被替换过的对象最多导致一次“跳过旧内容”（stale skip），绝不会拼出损坏文件。
  要强制要求证据，用 `--require-witness`，或用 `downctl verify --checksums` 离线复核。
- 快速：单文件多连接并行，可多文件并发。
- 通用：HTTP(S) / HuggingFace / GitHub Releases / S3 / SFTP（需 `downctl[sftp]`）/ 本地文件开箱即用。
- 可扩展：一切皆插件，任何人都能发布自己的源、传输、校验器或钩子。

> 状态：**pre-1.0**。下载内核与 CLI 已端到端可用，详见[设计文档](https://github.com/ThzxxArt/downctl/blob/master/docs/design.md)。

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
  位图精确记录哪些分片已完成，可续传时**只补缺失分片**；中断、`Ctrl+C`、`SIGTERM`/`kill`、
  甚至 `kill -9`/断电之后再运行同一条命令，会从最近的持久化检查点继续（默认每 30 秒
  落盘一次，可用 `--checkpoint` 调整；`--checkpoint 0` 只在退出时落盘，硬杀会丢失本次运行
  的进度）。检查点只适用于已知大小的多连接下载；未知大小或
  服务器不支持 Range 时走单流，没有位图，重跑会从头下载。
- **完成有据**：下载成功后会写 `<文件>.downctl-done` 边车（格式 v2，绑定大小、mtime、
  URL 身份，并记录对象版本见证；如校验过还会记录摘要）。
  **已知大小**的完成文件下次运行凭它直接跳过，不再依赖“文件大小刚好对”这种不可靠判断；
  WSL DrvFS 与 NTFS 上也不会因此重复下载。未知大小的单流文件没有位图，普通重跑会整体
  重下；加 `--skip-existing` 才会凭该标记跳过。
- **状态不串台**：位图与完成标记都绑定 URL 身份与**对象版本见证**（强/弱 `ETag`，
  退化时用 `Last-Modified`）。换 revision、换镜像对象、同名不同 URL 会丢弃旧账本；
  **URL 不变但内容被替换**同样会被识别：续传前比对见证，传输中的分片请求带 `If-Range`，
  且每个 `206` 的 `Content-Range` 必须精确覆盖请求区间，任一不符就整体重下，
  绝不把两个版本的字节拼进同一个文件（详见下文「对象被替换时会怎样」）。
- **防静默损坏**：中断残留的稀疏文件、中间有空洞的文件不会被误判为“已完成”，
  必要时重下，保证最终文件内容正确。
- **多连接加速**：单文件默认 16 连接并行，分片粒度约 8 MB（小文件自动变小）。
  已知大小的多连接下载在 `--checkpoint > 0` 时，硬杀最多回退一个检查点间隔；优雅退出
  （`Ctrl+C`/`SIGTERM`）会立即落盘；`--checkpoint 0` 只在退出时落盘。
- **断线自愈**：网络闪断、代理抖动、服务端卡死都会被“卡死看门狗”捕获并自动重连续传，
  直至文件完整，无需人工干预。
- **多文件并发**：`-j/--jobs` 并发下载多个文件。
- **校验**：支持单个 `--checksum ALGO:HEX`（`sha256`/`sha1`/`sha512`/`md5`），以及
  `--checksums`（`sha256sum`/`sha1sum`/`md5sum` 风格清单）。
- **镜像 / 代理**：`--mirror FROM=TO` 前缀改写；代理支持参数与环境变量；WSL 下自动探测
  宿主代理，且**只用于远程目标**（回环、链路本地与 `no_proxy` 命中一律直连）；`--no-proxy`
  可完全关闭代理。
- **内置源**：普通 URL、HuggingFace 仓库整仓、GitHub Releases、S3、SFTP。
- **一切皆插件**：9 类插件（源 / 传输 / 校验 / 进度 / 路由 / 钩子 / 作业源 / 凭据 /
  交付），内置即插件；`sink` 类别已于 0.1.4 移除（内核一直通过 `core/transfer.py`
  直接写入，该契约从未生效）。
- **跨平台**：Windows / macOS / Linux / WSL；CI 在 Linux、macOS、Windows 上以
  Python 3.11–3.14 运行测试。WSL 的路径翻译与代理作用域由单元/集成测试用平台模拟覆盖，
  没有独立的 WSL CI job；控制台颜色与编码由代码自动处理。

---

## 安装

### 环境要求

- **Python 3.11 或更高版本**（依赖 `asyncio.TaskGroup`、异常组等特性）。
- 运行时依赖：`aiohttp>=3.11`、`tqdm`（pip 会自动安装）。
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
python -m pip install 'downctl==0.1.6'

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

- **0.1.6 新增命令**：`downctl batch <文件>`（TOML/JSON 作业，退出码 `0/1/2`）、
  `downctl auth check <URL>...`（配合 `[auth."<主机>"]`，provider 为 `env`/`command`/
  `netrc`）、`downctl serve <目录|文件>`、`downctl plugin check/new`。
  详见[作业、凭据与交付](https://github.com/ThzxxArt/downctl/blob/master/docs/batch.md)。
- **0.1.6 行为变化**：`--no-proxy` 直连、`--limit-rate 0` = 不限速；`status` 有失败/
  blocked 目标时退出 `1`；`verify` 的“无法证明”改为退出 `3`（`2` 留给参数错误）；
  `-q` 仍打印命令结果；`config show` 打印生效配置并告警未知 `[core]` 键；失败清单 v6
  记录请求头名字与传输方式，重试时缺头会明确拒绝；`--require-witness` 不再信任“满大小、
  无空洞、无标记”的文件；manifest 改为合并写入。**状态无需迁移**：位图 v4、完成标记 v2
  不变，失败清单只是追加到 v6，旧记录仍可读。
- **0.1.5 行为变化**：HuggingFace 令牌默认**不**发给配置的 `mirror`（镜像需要鉴权时显式设
  `[plugins.huggingface] token_to_mirror = true`）；*猜测的* WSL 宿主代理只作用于远程目标；
  `checksum`/`manifest` 在 `witness = none` 时不写产物；`probe --json` 的 `witness` 字段
  更名为 `validator_kind`；来自更新版本的状态文件会被报告并原样保留（`--force` 才丢弃）。
- 升级**安装过程**不会删除你的下载文件、`~/.config/downctl/config.toml`、失败清单或完成标记；
  旧位图文件也不会在安装时被删除（但首次续传时可能因缺少 URL 身份被丢弃，见下）。
- 新增/变更的配置键向后兼容；未知键会被忽略（`config show` 可查看生效值）。
- 0.1.1 起版本号来自包内 `__version__`；用 `downctl version` 或 `pip show downctl` 核对。
- 0.1.3 起位图绑定 URL 身份（v4）。**0.1.2 及更早版本留下的未完成位图无法证明属于哪个 URL，
  首次续传时会被丢弃并重下**；已完成文件没有位图，不受 v4 影响。
- 0.1.3 起 Windows / WSL DrvFS 上“满大小但无完成标记”的文件不再被直接信任，
  会通过下载补齐（`DOWNCTL_TRUST_PARTIAL=1` 可一次性放行，请确认文件确实完整）。
  **这包括 0.1.2 及更早版本完成的文件**：它们没有 `.downctl-done` 标记，在这两类文件系统上
  首次重跑会重新下载一次；原生 Linux/macOS 可通过空洞扫描证明后补写标记，不会重下。
- 0.1.4 起位图与完成标记都增加**对象版本见证**字段，但**格式版本号没有变**（位图仍是 v4，
  标记仍是 v2——该字段是纯增量）。因此**降级回 0.1.3 也是安全的**：旧版本读到它时直接忽略，
  不会把文件当成“无法证明”的状态而截断。
  **0.1.3 留下的未完成位图没有见证**，所以当服务器能报出版本时（正常情况），首次续传会丢弃
  该账本、整个文件重下一次；服务器完全不提供见证时没有可比对的依据，行为与 0.1.3 一致，
  直接续传。已完成文件没有位图，不受影响。
- 0.1.3 时期的完成标记同样没有见证，继续被信任，因此**不会**造成大规模重下；下一次把这个
  目标真正跑到完成的运行会**原位补写**当时观察到的见证。没有见证的标记若只是被
  `--skip-existing` 再次跳过，会报告为 `unversioned` 并**刻意保持无见证**——把今天的见证盖到
  一份在未知版本下取得的文件上，会让下一次替换再也无法被发现
  （`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`）。
- 0.1.4 起 `sink` 插件类别与 `downctl.sinks` entry-point 组退役；安装包若仍声明它，
  加载时会打印警告并忽略，`downctl plugin list` 不再列出 `sink`。0.1.4 时存在的其余类别
  （当时六类，今天注册表定义九类）不受影响。
- 0.1.4 起重试次数有限（默认 10 次）：要恢复“无限重试”请设 `-r 0` 或
  `[core] retries = 0`；探测阶段无论怎样都保持有界（最多 3 次尝试）。
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
downctl get <url> -r 0      # 0 = 无限重试（默认为 10 次，配置键 [core] retries）
```

探测（resolve/probe）阶段的重试**始终有界**：最多 3 次尝试，退避与服务器给出的
`Retry-After` 都封顶 5 秒，`-r 0` 也不会让探测无限重试。
`--max-time SECONDS` 可以给单个目标设墙钟上限（`0` = 不限）。

### 断点续传与中断恢复

`downctl` 默认就支持断点续传，**不需要额外开关**：

- 下载过程中会在目标文件旁写一个位图 `<文件名>.downctl-pieces`（格式 v4，绑定 URL 身份与
  对象版本见证），记录每个分片的状态；
- 中断（`Ctrl+C`、网络断开、机器重启）后，重新运行**完全相同的命令**即可从断点继续，
  只会补下载缺失的分片；
- 完成后位图自动删除；
- 用 `downctl status <目录>` 可以查看哪些文件未完成、进度百分比与失败清单。

卡死看门狗：若连续 `--stall`（默认 30）秒无任何进展，会自动断开并重连续传。

### 对象被替换时会怎样

位图（`.downctl-pieces`，格式 v4）与完成标记（`.downctl-done`，格式 v2）都会记录远程对象的
**版本见证**：强 `ETag`、弱 `ETag`，或退化的 `Last-Modified`；服务器都不给时记为“无见证”。
URL 身份回答“是不是同一个地址”，见证回答“是不是同一份字节”，两者都需要——地址可以不变，
内容却已被替换（重新上传的 release 资产、被覆盖的模型分片）。

| 情形 | downctl 的行为 |
|---|---|
| 版本没变（见证一致） | 续传：只补缺失分片；已完成的文件凭标记直接跳过，不重新下载 |
| 两次运行之间被替换 | 账本见证不一致 → 丢弃账本、**从零重下**；完成标记同样作废，`--skip-existing` 不会把旧内容当成“已证明” |
| 传输过程中被替换 | 分片请求带 `If-Range`；若服务器对条件请求回了完整正文，再做一次探测区分：不支持 Range 就走单流，对象已换就**从零重启一次**（`ContentChangedError`；再次发生则报错放弃） |
| 服务器不给见证 | 无法比对版本：已完成文件仍可跳过，但会明确提示 `unversioned`，钩子与干跑 JSON 里 `"witness": "none"`；`--require-witness` 直接拒绝这种跳过与续传 |

对应回归测试：相同版本续传与换版本不拼接见
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`、
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`、
`tests/integration/test_content_truth.py::test_cli_run_does_not_splice_a_replaced_object`；
传输中替换见 `tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`；
无见证跳过见 `tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`。
服务器每次响应都换见证（例如每节点一个 `ETag`）会被识别，该目标放弃见证继续下载
（报告为 unversioned）而不是无限重启：
`tests/integration/test_content_truth.py::test_unstable_etag_still_downloads`；
弱 `ETag` 只用于比对、绝不放进 `If-Range`：
`tests/integration/test_content_truth.py::test_normalize_validator_never_sends_a_weak_etag_conditionally`。

### 校验

```bash
# 单文件：下载后校验 sha256，不匹配自动重下（超限后放弃）
downctl get <url> --checksum sha256:<64位十六进制>

# 批量：按校验和清单校验（每行 "<hex>  <文件名>"，支持 sha256/sha1/sha512/md5）
downctl get <url> --checksums ./SHA256SUMS
```

`manifest` 钩子可以自动为已完成的下载生成这样一份清单（见 [钩子](#内置钩子)）。

下载完成后可以离线复核：`downctl verify [目录]` 为每个带完成标记的文件给出
`ok` / `mismatch` / `unverifiable` / `missing` 与所用见证；退出码 `0` = 检查过的文件全部
得到证明、`1` = 有内容不符或数据文件缺失、`3` = 没发现错误但无法证明。
详见[命令行参考](https://github.com/ThzxxArt/downctl/blob/master/docs/cli.md)。

### 代理

```bash
downctl get <url> -p http://127.0.0.1:7897
```

也会自动读取环境变量 `https_proxy` / `HTTPS_PROXY` / `http_proxy` / `HTTP_PROXY`。
在 WSL 下若未显式指定代理，会自动探测 Windows 宿主 IP 并启用宿主代理；该猜测只作用于
远程目标，回环、链路本地与 `no_proxy` 命中一律直连。`--no-proxy` 忽略所有代理直接连接。

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
包括 HuggingFace / GitHub 仓库里的子目录路径与预期校验和。清单同时记录你最初给的
目标，因此 S3 会重新签名、私有仓库会带上当前 token，而不是重放过期地址。

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
  用于私有仓库 / 提高限额。令牌只发给 `huggingface.co`；镜像需要鉴权时显式设置
  `token_to_mirror = true`（默认 `false`，因令牌被扣下而收到的 401 会点名这个键）。
- 新增 `http_index` 源：以 `/` 结尾的 http(s) URL 会被当作目录，解析 HTML 或 JSON
  列表并应用 `--include/--exclude`（`https://host/dir/`）。

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
| `downctl batch <文件> [--job NAME]... [--dry-run] [--continue]` | 运行 TOML/JSON 作业文件；退出码 `0`/`1`/`2`（见[作业、凭据与交付](https://github.com/ThzxxArt/downctl/blob/master/docs/batch.md)） |
| `downctl auth check <URL>... [--json]` | 显示主机命中的 `[auth]` 规则与 provider（只显示材料名字） |
| `downctl serve [<目录\|文件>] [--bind ADDR] [--port N] [--auth USER:PASS] [--index] [--quiet-logs]` | 以可续传的 HTTP 语义交付本地文件 |
| `downctl probe <目标...> [-p PROXY] [--json]` | 只探测大小 / HTTP 状态，不下载；不可达、HTTP ≥400 或非 HTTP(S) 时退出码为 1 |
| `downctl status [目录] [--json]` | 显示未完成（位图）、失败与 blocked；有失败/blocked 时退出 1 |
| `downctl verify [目录] [--json] [--checksums FILE] [--repair]` | 离线复核完成标记：`ok`/`mismatch`/`unverifiable`/`missing`，退出码 `0`/`1`/`3`（`2` 留给参数错误） |
| `downctl clean [目录] [--dry-run] [--json]` | 清理残留位图 / 失败清单 / 带 `downctl-lock` 标记的锁目录 |
| `downctl plugin list` | 按类别列出已发现插件 |
| `downctl plugin info <名称>` | 显示类别、优先级、origin、api_version、选项与能力 |
| `downctl plugin check <模块或发行版>` | 加载插件并运行契约检查；有问题退出 1 |
| `downctl plugin new <名称> --kind KIND [--dir DIR]` | 生成可工作的插件包 |
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
| `--checkpoint N` | 续传状态落盘间隔秒数（默认 30；`0` = 仅在退出时） |
| `--piece-size SIZE` | 分片大小，如 `8M` / `4M`（默认自动） |
| `--min-split-size SIZE` | 小于该大小的文件走单连接 |
| `-t, --limit-rate RATE` | 限速，如 `5M` / `1m` / `512k`；`0` = 不限速 |
| `-r, --retries N` | 每个文件最大重试次数（默认 10；`0`=无限，探测阶段仍始终有界） |
| `--max-time SECONDS` | 单个目标的墙钟上限（`0` = 不限；超时后记入失败清单） |
| `--probe-jobs N` | 解析/探测阶段的并发目标数（默认 8；`0` = 跟随 `--jobs`）；失败仍按命令行顺序报告 |
| `--force` | 忽略已有文件/续传状态，从头重下 |
| `--skip-existing` | 跳过**能证明已完成**的文件（完成标记 / 完整位图 / 可验证无空洞；带 `--checksum`/`--checksums` 时还会复验）；无见证的跳过会提示 `unversioned` |
| `--require-witness` | 拒绝跳过或续传**版本无法核对**的状态（服务器不给 `ETag`/`Last-Modified` 时宁可重下） |
| `--recheck` | 跳过前先复核已有文件：有摘要就重新计算，没有则要求版本见证（隐含 `--require-witness`） |
| `--checksum ALGO:HEX` | 下载后校验 |
| `--checksums FILE` | 按 `sha256sum`/`sha1sum`/`sha512sum`/`md5sum` 风格文件校验 |
| `-p, --proxy URL` | 代理 |
| `--no-proxy` | 直连，忽略配置/环境变量里的代理（以及 WSL 猜测） |
| `-H, --header K:V` | 附加请求头（可多次） |
| `-u, --user USER:PASS` | HTTP Basic 认证 |
| `--mirror FROM=TO` | URL 前缀改写 |
| `--source NAME` | 强制指定源插件 |
| `--reporter NAME` | 进度呈现（`tqdm`/`json`/`quiet`） |
| `--include PAT` / `--exclude PAT` | 仓库/前缀源的过滤通配（可多次） |
| `--revision REV` | 仓库 revision |
| `--dry-run` | 只探测不下载（也不写入或修改下载状态）；JSON reporter 下每个目标输出一条 `dry_run` 记录（`size`/`done`/`pending`/`witness`） |
| `--retry-failed` | 重试失败清单（按记录的原始目标重新解析，S3 会重新签名、私有仓库会重新取 token） |
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
| `1` | 有一个或多个目标失败；`status` 有失败/blocked、`batch` 有作业失败时同样为 1 |
| `2` | 仅 `batch`：作业文件非法（未下载）；argparse 用法错误也是 2 |
| `3` | 仅 `verify`：没有发现问题，但无法证明 |
| `130` | 被中断（进度已保留，重跑即续传） |

`downctl probe` 与 `downctl verify` 有自己的退出码：`probe` 在所有目标可用时返回 0，
有不可达、HTTP ≥400 或非 HTTP(S) 目标时返回 1；`verify` 返回 0（全部得到证明）、
1（有 `mismatch` 或有完成标记却没有数据文件）、3（没有发现错误，但存在无法证明的文件）。

---

## 配置文件

默认路径：`$XDG_CONFIG_HOME/downctl/config.toml`（未设置时为
`~/.config/downctl/config.toml`；可用环境变量 `DOWNCTL_CONFIG` 覆盖）。
值支持 `${环境变量}` 插值。命令行参数优先于配置文件。

```toml
[core]
dir = "~/Downloads"     # 默认保存目录
jobs = 2                # 并发文件数（默认 1，此处仅为示例）
retries = 10            # 每文件重试次数（0 = 无限；探测阶段始终有界）
connections = 16        # 单文件连接数
stall = 30              # 卡死看门狗秒数
checkpoint = 30         # 续传状态落盘间隔（秒，0 = 仅退出时）
# max_time = 0          # 单目标墙钟上限（秒，0 = 不限）
# probe_jobs = 8        # 解析/探测并发（0 = 跟随 jobs）
# require_witness = false  # 拒绝跳过/续传版本无法核对的状态
# piece_size = 8388608  # 分片字节数（默认自动）
# min_split_size = 1048576  # 小于此值走单连接
# rate = 5242880        # 限速（字节/秒）
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["logfile", "manifest"]   # 每次运行启用的钩子

[auth."huggingface.co"]
# provider = "env"
# env = "HF_TOKEN"

[auth."private.example.com"]
# provider = "command"
# command = "pass show example/token"

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
| `XDG_CONFIG_HOME` | 默认配置与本地插件目录的基准目录 |
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
| Verifier | 下载后校验 | `sha256`、`sha1`、`sha512`、`md5` |
| Reporter | 进度与日志 | `tqdm`、`json`、`quiet` |
| Router | URL 改写 | `mirror` |
| Hook | 生命周期事件 | `logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum`、`metrics`、`notify` |
| Job source | 读取批量作业文件 | `toml`、`json` |
| Auth provider | 为主机提供凭据 | `env`、`command`、`netrc` |
| Server | 交付文件给另一台机器 | `http` |

**发现机制**（后者可按名称覆盖前者）：

1. 内置 `downctl.builtin`；
2. 已安装包通过九个 `downctl.*` entry-point 组声明（内置插件不再声明为 entry point）；
3. 本地目录 `./downctl_plugins` 与 `$XDG_CONFIG_HOME/downctl/plugins`
   （未设置时为 `~/.config/downctl/plugins`；需 `--local-plugins`）。

0.1.4 起 `downctl.sinks` entry-point 组已退役：仍声明它的安装包会在加载时收到一条
警告（而不是被静默忽略），0.1.4 时存在的其余类别（当时六类，今天注册表定义九类）不受影响
（`API_VERSION` 仍为 `"1"`）。

### 编写插件

最小示例见 [`examples/plugins/`](https://github.com/ThzxxArt/downctl/tree/master/examples/plugins)。插件是任意对象，只需暴露
`name` / `api_version` / `priority` 以及某个契约的方法（见
[`downctl.plugin.protocols`](https://github.com/ThzxxArt/downctl/blob/master/src/downctl/plugin/protocols.py)）：

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
  并做**路径穿越防护**（tar 还拒绝链接/设备/FIFO；zip 只做路径校验）。
  `into = "目录"`（默认同名兄弟目录）、`keep = true/false`。
- **`webhook`**：把选定事件以 JSON POST 出去（fire-and-forget）。
  `url`、`events`（默认 `["on_finish"]`）、`timeout`。
- **`manifest`**：为已完成的下载写一份 `sha256sum` 清单，可日后配合 `--checksums` 复验。
  `filename`（默认 `.downctl.manifest`）。
- **`exec`**：事件触发时执行 shell 命令，事件信息通过 `DOWNCTL_EVENT`/`DOWNCTL_TARGET`/
  `DOWNCTL_PATH`/`DOWNCTL_URL`/`DOWNCTL_ORIGIN`/`DOWNCTL_OK` 环境变量传入
  （`DOWNCTL_URL` 已脱敏，不会把预签名串交给子进程）。
  `command`（必填）、`events`（默认 `["on_task_finish"]`）、`timeout`（默认 300）。
- **`checksum`**：为每个已完成的下载写一份 `<文件>.<算法>` 边车（`sha256sum` 行格式）。
  `algo`（默认 `sha256`）、`events`（只接受 `on_task_finish`）。
- **`metrics`**：`on_finish` 时把运行统计写成 Prometheus textfile。
  `path`（默认 `downctl.prom`）、`labels`。
- **`notify`**：通过 `url` 或 `command` 发送一行运行摘要；
  `events`、`only_failures`、`title`。`url` 与 `command` 只能设一个。

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
不会。默认按分片位图续传，重跑相同命令只补缺失分片。多连接下载每
`--checkpoint` 秒（默认 30）把“哪些分片已落盘”持久化一次，因此 `kill -9`、
断电最多回退一个检查点间隔；`--checkpoint 0` 只在退出时落盘，硬杀会丢失本次运行的进度。
`Ctrl+C`、`SIGTERM`（POSIX；Windows 控制台进程没有可捕获的 SIGTERM）会走优雅退出，
立即落盘。
（`DOWNCTL_TRUST_PARTIAL=1` 仅影响“大小已达预期、但无法探测稀疏空洞”的本地文件判定，
不是用来续传比预期更小的部分文件的。未知大小的单流下载没有分片账本，只能整体重下。）

**重复运行会不会重新下载？**
已知大小的完成文件不会：它们带 `<文件>.downctl-done` 标记，重跑时凭它直接跳过。
未知大小或服务器不支持 Range 的单流下载没有分片账本，普通重跑会整体重下；
加 `--skip-existing` 才会凭完成标记跳过。
如果文件是别的工具放的、没有标记：Linux 原生文件系统会做空洞探测；WSL DrvFS、
Windows 与“文件被外部改过”的情况则宁可从源补齐，也不赌内容完整
（`--skip-existing` 与普通重跑都遵循这一规则）。
跳过只在**能证明**时发生，并且会说明用的是哪种见证：`validator`（版本见证一致）、
`checksum`（摘要复核通过）或 `none`（无见证）。`none` 的跳过仍然允许（它不会拼出坏文件），
但会明确提示 `unversioned`；要拒绝这类跳过就用 `--require-witness`。

**文件明明已经有了，为什么又下了一次？**
三种可能：① 服务器报告的版本见证变了（对象被重新上传），旧账本/旧完成标记作废——这正是
0.1.4 要修的问题；② 用了 `--require-witness`（或 `[core] require_witness = true`），
拒绝跳过或续传任何版本无法核对的状态；③ 用了 `--recheck`，跳过前先复核（有摘要就重算，
没有则要求版本见证）。三种情况都只影响该文件，不会造成整批重下。

**怎么离线证明已有文件是对的？**
`downctl verify <目录>` 检查每个带 `.downctl-done` 标记的文件，报告
`ok` / `mismatch` / `unverifiable` / `missing` 与所用见证；退出码 `0` = 全部有证据、
`1` = 有内容不符或有标记却没有数据文件、`3` = 没发现错误但无法证明（例如标记里没有摘要）。
加 `--checksums SHA256SUMS`，只要清单里有对应条目，就能把 `unverifiable` 变成证明
（普通 `*sum` 文件按摘要长度识别 md5/sha1/sha256/sha512）；`--repair` 只删除**已被证明**
坏掉的文件与孤儿标记，是破坏性操作。

**提示磁盘空间不足？**
下载前会做磁盘预检（保留 200 MiB 余量），并在整批开始前按**文件系统**汇总每个目标还需要
多少字节：整批放不下时会一次性失败，报出“需要多少、可用多少”，一个字节都不会写
（并发数不会因此降低：已经写下的字节仍然占着磁盘）。单个文件空间不足时只失败当前文件，
不影响其它文件。

**探测失败，或者 `--max-time` 提前放弃了？**
探测阶段的 5xx/429/408/425 与网络故障会重试，最多 3 次尝试，退避与 `Retry-After` 都封顶
5 秒；401/403/404 等 4xx 是最终答案，立即失败并记入失败清单。
`--max-time N` 到点后该目标以 `exceeded --max-time Ns` 记入 `.downctl.failed`，
已下载的分片保留，重跑即续传。

**HuggingFace / GitHub 报 401/403/404？**
多为私有资源或匿名限流：配置相应 `token` 或用对应环境变量。`--mirror` 是 URL 改写
而非凭据，不能代替令牌——公开资源被限流时可以用它走镜像。HuggingFace 令牌只发给
`huggingface.co`；镜像需要鉴权时必须显式设置
`[plugins.huggingface] token_to_mirror = true`（默认 `false`），否则镜像收到的是不带
令牌的请求。

**代理相关？**
用 `-p` 或环境变量；WSL 下会自动探测宿主代理，但**只用于远程目标**（回环、链路本地与
`no_proxy` 命中一律直连），必要时用 `WSL_HOST_IP` 手工指定。`downctl doctor` 会显示
探测到的代理及其来源与作用域。

**卡在某个文件不动？**
`--stall` 秒数内无进展会自动断开重连；若服务端长期不稳定，可调大或调小该值。

**如何查看还有哪些没下完？**
`downctl status <目录>`；失败项可 `downctl get --retry-failed` 重试。
`status --json` / `clean --json` 供脚本消费。

**`--retry-failed` 对 S3、私有 HuggingFace/GitHub 有效吗？**
有效。失败清单记录的是用户最初的目标（`s3://bucket/key`、`hf://org/repo@rev` 等），
重试时会重新解析并生成新的预签名 URL / 带上当前 token，而不是重放已过期的地址。
0.1.2 及更早版本留下的旧记录没有这一信息，会提示你重跑原始目标。

**`probe` 的退出码？**
所有目标都可用返回 `0`；有不可达、HTTP ≥400 或非 HTTP(S) 目标返回 `1`
（文本与 `--json` 都会给出原因）。

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
# 关键状态机的分模块门槛（CI 会逐个执行）
coverage report --include="*/downctl/core/pieces.py"   --fail-under=95
coverage report --include="*/downctl/core/transfer.py" --fail-under=95
coverage report --include="*/downctl/core/engine.py"   --fail-under=95
python -m build       # 构建 sdist / wheel
```

测试里有几条"内容不变式"用例会真实起子进程（`SIGTERM`、`kill -9`）并比对最终文件的
SHA-256；它们比其余用例慢，但正是它们让"绝不静默损坏"这句话可被证伪。

---

## 文档与许可

- [命令行参考](https://github.com/ThzxxArt/downctl/blob/master/docs/cli.md)
- [下载源](https://github.com/ThzxxArt/downctl/blob/master/docs/sources.md)
- [配置](https://github.com/ThzxxArt/downctl/blob/master/docs/configuration.md)
- [钩子](https://github.com/ThzxxArt/downctl/blob/master/docs/hooks.md)
- [插件开发](https://github.com/ThzxxArt/downctl/blob/master/docs/plugins.md)
- [故障排查](https://github.com/ThzxxArt/downctl/blob/master/docs/troubleshooting.md)
- [实战配方](https://github.com/ThzxxArt/downctl/blob/master/docs/cookbook.md)
- [设计文档](https://github.com/ThzxxArt/downctl/blob/master/docs/design.md)
- [贡献指南](https://github.com/ThzxxArt/downctl/blob/master/CONTRIBUTING.md) · [安全策略](https://github.com/ThzxxArt/downctl/blob/master/SECURITY.md) · [发布流程](https://github.com/ThzxxArt/downctl/blob/master/RELEASING.md)

本项目基于 [MIT 许可证](https://github.com/ThzxxArt/downctl/blob/master/LICENSE) 发布，© ThzxxArt。
