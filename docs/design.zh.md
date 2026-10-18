# downctl 设计文档

> 一个基于插件架构的通用命令行文件下载器。
> 具备断点续传、多连接加速、断线自愈重试等能力，一切皆插件。

- **项目名**：`downctl`
- **命令**：`downctl`
- **仓库**：https://github.com/ThzxxArt/downctl
- **作者**：ThzxxArt
- **许可证**：MIT
- **最低 Python**：3.11

---

## 1. 定位

downctl 是一个「通用」文件下载器：给定任意可下载目标，尽可能可靠、可续传、
可加速地把数据落到本地。它不绑定任何单一站点——对 HuggingFace 的支持只是
内置的一个「源插件」，与第三方插件走完全相同的接口。

**核心理念：一切皆插件。**

---

## 2. 核心理念的务实边界

「一切皆插件」必须区分两类东西，否则会把有状态的紧耦合算法拆得又慢又脆：

### 2.1 插件层（可变、可替换）

所有「策略型」能力都通过统一插件接口实现，**内置功能也使用同一套接口**
（dogfooding），第三方可以通过 pip 安装包替换或扩展：

| 类型 | 职责 |
|---|---|
| Source | 把用户输入解析成下载任务列表 |
| Transport | 实际的数据传输 |
| Verifier | 下载后校验 |
| Reporter | 进度与日志呈现 |
| Router | URL 改写 / 镜像切换 |
| Hook | 生命周期事件回调 |

> `Sink` 类别已于 0.1.4 退役：内核从始至终通过 `core/transfer.py` 直接做区间写入，
> 该契约承诺了可替换的落地方式却从未生效。`downctl.sinks` entry-point 组随之退役——
> 仍声明它的安装包会在加载时收到明确警告（而不是被静默忽略）；`API_VERSION` 仍为
> `"1"`，其余六类插件不受影响。

### 2.2 内核层（稳定、不插件化）

以下能力是**有状态的紧耦合状态机**，拆成插件只会带来性能损耗与脆弱性，
因此作为稳定的 **Core API** 暴露给插件调用，本身不是插件：

- 分片调度与并发控制
- 断点续传位图（piece bitmap，格式 v4：绑定 URL 身份与对象版本见证）
- 条件区间请求（`If-Range`）与 `Content-Range` 校验
- 卡死看门狗（stall watchdog）
- 限速（rate limit）
- 单文件 / 单实例锁
- 磁盘空间预检（单文件 + 整批预算）

> 一句话：**内核是引擎，插件是齿轮；不是把引擎也拆成齿轮。**

---

## 3. 目录结构

```
downctl/
├── src/downctl/
│   ├── __init__.py              # __version__、公共 API、API_VERSION
│   ├── __main__.py              # python -m downctl
│   ├── cli.py                   # argparse 子命令 + 分发
│   ├── app.py                   # 应用编排（加载插件→建任务→跑引擎）
│   ├── config.py                # TOML 配置 + 插件命名空间
│   ├── probe.py                 # 大小/状态探测
│   ├── platform_utils.py        # WSL/VT 颜色/安全 IO/pid 检活/SIGTERM
│   ├── urls.py                  # URL 脱敏 + 内容身份（sha256）
│   ├── plugin/
│   │   ├── __init__.py
│   │   ├── protocols.py         # 六类插件 Protocol 定义
│   │   ├── registry.py          # 注册表 + 选择/优先级
│   │   ├── loader.py            # entry_points + 本地目录发现 + 退役组警告
│   │   └── hooks.py             # 生命周期事件总线
│   ├── core/                    # ★ 稳定内核（不插件化）
│   │   ├── __init__.py
│   │   ├── engine.py            # 调度 + 多次重试编排
│   │   ├── transfer.py          # 分片下载（If-Range / Content-Range 校验）
│   │   ├── pieces.py            # 分片位图（格式 v4：URL 身份 + 版本见证）
│   │   ├── disk.py              # 磁盘预检（单文件 + 整批预算）
│   │   ├── watchdog.py          # StallTimeout
│   │   ├── ratelimit.py         # 严格限速（无空闲信用）
│   │   ├── locks.py             # 目录锁
│   │   ├── task.py              # DownloadTask 数据模型
│   │   ├── context.py           # RuntimeContext（注入 session/配置/事件）
│   │   └── errors.py            # 异常定义
│   └── builtin/                 # 内置插件（全部走插件接口）
│       ├── __init__.py
│       ├── sources/{url, huggingface, github, s3, sftp, file}_source.py, _sigv4.py
│       ├── transports/{http, sftp, file}_transport.py, _sftp.py
│       ├── verifiers/{sha256, digest}_verifier.py
│       ├── reporters/{tqdm, json, quiet}_reporter.py
│       ├── routers/{mirror_router.py}
│       └── hooks/{logfile, archive, webhook, manifest, exec, checksum}_hook.py
├── tests/{unit/, integration/}
├── docs/
├── examples/plugins/            # 第三方插件示例
├── .github/workflows/{ci.yml, docs.yml, release.yml, codeql.yml}
├── mkdocs.yml
├── pyproject.toml
├── README.md / README.en.md
├── CHANGELOG.md / CONTRIBUTING.md / SECURITY.md / RELEASING.md / CODE_OF_CONDUCT.md
└── LICENSE
```

---

## 4. 插件体系

### 4.1 六类插件

| 类型 | entry-point group | 内置实现 |
|---|---|---|
| Source | `downctl.sources` | `url`、`huggingface`、`github`、`s3`、`sftp`、`file` |
| Transport | `downctl.transports` | `http`、`sftp`、`file` |
| Verifier | `downctl.verifiers` | `sha256`、`sha1`、`sha512`、`md5` |
| Reporter | `downctl.reporters` | `tqdm`、`json`、`quiet` |
| Router | `downctl.routers` | `mirror` |
| Hook | `downctl.hooks` | `logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum` |

第七类 `Sink`（`downctl.sinks`、内置 `filesystem`）已于 0.1.4 退役：它从未被写入路径
调用，退役后加载器会对仍声明该组的包给出警告，`downctl plugin list` 也不再列出 `sink`。

### 4.2 发现机制（优先级从高到低）

1. **本地目录**：`./downctl_plugins/` 与 `~/.config/downctl/plugins/`
   （仅显式传 `--local-plugins` 时加载；不受信代码）
2. **pip 包**：通过 `importlib.metadata` 读取 entry-points
3. **内置**：`downctl.builtin`

### 4.3 选择机制

- 按 **URI scheme 路由**：`https://`→`http`，`hf://org/repo`→`huggingface`，
  `gh://owner/repo`→`github`，`s3://bucket/key`→`s3`，`sftp://host/path`→`sftp`，
  `file:///path`→`file`。
- 可用 `--source` 强制指定。
- 同类多插件按 `priority` + `can_handle(target)` 判定。

### 4.4 API 版本化

`downctl.API_VERSION = "1"`。每个插件声明其兼容的 `api_version`；
加载时不匹配则拒绝并给出清晰提示。

### 4.5 插件协议（示意）

```python
class Source(Protocol):
    name: str
    priority: int
    api_version: str

    def can_handle(self, target: str) -> bool: ...
    async def resolve(self, target: str, ctx: "RuntimeContext") -> list["DownloadTask"]: ...
```

内核通过 `RuntimeContext` 向插件注入：HTTP 会话、配置、事件总线、
取消信号等运行时依赖。

---

## 5. 断点续传与内容可信

- 下载按约 8 MiB 分片，单文件最多 `connections` 个并发。
- **分片位图**（`<文件>.downctl-pieces`）是“哪些分片已完成”的权威账本：多连接下载分片
  乱序完成，文件大小无法描述续传状态，位图可以。
- 账本绑定 **URL 身份**（规范化 URL 的 SHA-256；S3 预签名 query 被排除）。属于别的 URL
  的位图，或没有身份信息的 v4 之前位图，一律丢弃，绝不续传到别的对象上。
- 账本还绑定对象的**版本见证**（0.1.4 新增的纯增量字段，格式版本刻意保持 `4`，好让降级到
  0.1.3 时被忽略而不是截断文件）：服务器给出的强 `ETag`、弱 `ETag`，或退化的
  `Last-Modified`；都没有时留空（unversioned）。见证与本次观察不一致的账本会被丢弃，
  绝不与当前 revision 拼接。归一化由 `urls.normalize_validator` 负责：弱 `ETag` 仍可作
  比对键，但绝不会放进 `If-Range`（RFC 9110 要求强校验器）。
- **每个** HTTP(S) 目标都会为取得见证探测一次，**包括源已经报出大小的目标**（S3 列表、
  HuggingFace API 响应、GitHub asset）。正是这次探测让传输变成有条件的、让完成声明可被核对；
  过去对已知大小跳过它，结果是传输发无条件的区间请求、写出的标记永远发现不了后来的替换。
  代价只是每个目标一次 `HEAD`。探测发送 `Accept-Encoding: identity` 以与传输保持一致；
  当 `HEAD` 不可用时回退到带区间的 `GET`——包括**方法级**的 `401`/`403`：预签名 URL 签名
  绑定 HTTP 方法，所以 GET 签名的链接会对 `HEAD` 回 403，而对传输实际使用的带区间 `GET`
  正常响应。
- 只要已知强见证，分片请求就是**有条件的**（`If-Range`）；每个 `206` 的 `Content-Range`
  必须精确覆盖请求区间，否则在写下任何字节之前就被拒绝。条件请求收到完整正文（`200`）时，
  用一次额外探测区分两种可能：服务器根本不支持 Range（回退单流），或对象已被替换
  （`ContentChangedError`，引擎据此从零重启一次）。
- 中断时先把数据 `fsync`，再原子写入位图；重试只补缺失分片。传输过程中每 `checkpoint`
  秒（默认 30）落盘一次，因此硬杀进程最多丢一个间隔。原子改名之后会真正 `fsync` 目录：
  0.1.4 修掉了 `pieces._fsync_dir`——它此前用 `open(dir, "rb")` 打开目录，在 POSIX 上抛
  `IsADirectoryError` 却被静默吞掉，等于从未生效。
- 位图记录分片大小（v3），因此改 `--connections` / `--piece-size` 会按字节区间**重切**
  完成状态，而不是丢弃进度。
- 完成时写 `<文件>.downctl-done`（大小 + mtime + URL 身份 + 版本见证；见证同样是纯增量字段，
  标记格式保持 `2`，0.1.3 时期没有见证的标记继续被信任并报告为 `unversioned`）。只有该
  标记、完整位图或“可证明无空洞”的文件才能让引擎跳过已有文件——绝不凭大小。文件被外部
  修改会让标记失效。每次跳过都会说明所用见证：`validator`、`checksum` 或 `none`；完全没有
  见证时会明确提示 `unversioned`，`require_witness` 则直接拒绝。
- 进度插件可以按续传点起跳：引擎从标记/位图算出已完成字节，调用可选的 `run_resumed` /
  `task_resumed`。
- 没有标记时，满大小文件只有在能证明无空洞（`SEEK_HOLE`）或用户显式设
  `DOWNCTL_TRUST_PARTIAL=1` 时才被信任；WSL DrvFS 与 Windows 刻意不信任无标记的满大小文件。

### 5.1 失败恢复

失败清单记录解析后的 URL、目标、预期校验和/大小、源插件，以及 v3 起的**原始目标**。
`--retry-failed` 重新解析原始目标（刷新 S3 预签名与私有仓库 token），再按目标/URL 基名
匹配任务；没有原始目标的旧记录仍可加载，但会提示记录的 URL 可能已过期。

### 5.2 对象被替换时会怎样

URL 身份回答“是不是同一个地址”，版本见证回答“是不是同一份字节”，两者都需要：重新上传的
release 资产、被覆盖的模型分片都会保持 URL 不变而内容改变。两类记录刻意采用不同规则：
**位图**是续传记录，见证不一致就丢弃（复用那些分片正是“拼接两个 revision”的成因）；
**完成标记**是完成记录，只有双方都带见证时才可能比对。标记这条较弱规则的最坏结果是
**跳过旧内容**，绝不会拼出坏文件——而更严格会让“服务器开始或停止发送 ETag”变成全量重下。
`--require-witness` 则对两者都启用严格规则。跳过不会顺手盖上当天观察到的见证，除非**让这次
跳过成立的证据本身就是见证**（`validator`）或校验过的摘要（`checksum`）：见证出现之前写下的
标记——既无 validator 也无摘要——继续被信任（升级不能变成全量重下），只有之后真正把目标跑到
完成的运行才会为它补上见证
（`tests/integration/test_content_truth.py::test_a_legacy_marker_keeps_being_trusted`）。

| 情形 | 行为 |
|---|---|
| 版本没变（见证一致） | 续传：只补缺失分片；已完成文件凭标记跳过 |
| 两次运行之间被替换 | 账本见证不一致 → 丢弃账本、**从零重下**；标记同样被拒，`--skip-existing` 不能给旧内容背书 |
| 传输过程中被替换 | `If-Range` 不再匹配 → `ContentChangedError` → **从零重启一次**（再次发生则该目标失败） |
| 服务器不给见证 | 无从比对；跳过仍允许但报告为 `unversioned`（`"witness": "none"`），`--require-witness` 拒绝 |

四种情形的回归测试在
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`、
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`、
`tests/integration/test_content_truth.py::test_cli_run_does_not_splice_a_replaced_object`、
`tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`
与
`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`。
弱 `ETag` 只用于比对、绝不放进 `If-Range`
（`tests/integration/test_content_truth.py::test_normalize_validator_never_sends_a_weak_etag_conditionally`）；
每次响应都换见证的服务器会被识别，该目标放弃见证继续下载而不是无限重启
（`tests/integration/test_content_truth.py::test_unstable_etag_still_downloads`）。

离线证明是另一条通道：`downctl verify [DIR]` 判定每个带可识别完成标记的文件，报告
`ok` / `mismatch` / `unverifiable` / `missing` 与所用见证，退出码 `0`（全部得到证明）、
`1`（有内容不符，或有标记却没有数据文件）、`2`（没有发现问题但无法证明）。
`--checksums FILE` 提供把 `unverifiable` 变成证明所需的摘要；`--repair` 只删除已被证明
坏掉的东西。

---

## 6. CLI 设计

```
downctl get <target> [<target>...]    # 下载，按 scheme 自动路由源插件
    -d/--dir DIR         保存目录
    -o/--output PATH     保存为指定完整路径（单文件）
    -j/--jobs N          并发文件数
    --connections N      单文件连接数（默认 16）
    --stall SECONDS      卡死看门狗（默认 30）
    --checkpoint SECONDS 续传状态落盘间隔（默认 30）
    --piece-size SIZE    分片大小（如 8M）
    --min-split-size SIZE 小于该大小走单连接
    -t/--limit-rate RATE 限速，如 5M / 1m / 512k
    -r/--retries N       最大重试次数（默认 10；0=无限）
    --max-time SECONDS   单目标墙钟上限（0 = 不限）
    --probe-jobs N       解析/探测并发（默认 8；0 = 跟随 --jobs）
    --force / --skip-existing  强制重下 / 仅跳过可证明完成的文件
    --require-witness    拒绝跳过或续传版本无法核对的状态
    --recheck            跳过前重新复核已有文件
    --checksum ALGO:HEX  下载后校验单个目标
    --checksums FILE     按 *sum 风格清单校验
    -p/--proxy URL       代理地址
    -H/--header K:V      附加请求头（可多次）
    -u/--user USER:PASS  HTTP Basic 认证
    --mirror FROM=TO     镜像改写
    --source <name>      强制指定源插件
    --reporter <name>    指定进度呈现插件
    --hook NAME          本次运行启用某钩子（可多次）
    --user-agent UA      自定义 User-Agent
    --dry-run            只探测不下载
    --retry-failed       仅重试上次失败项
    -l/--list FILE       从文件/标准输入读取目标（- = stdin）
    --continue           单个目标解析/探测失败时跳过

downctl probe <target> [--json]       # 只探测大小/状态/见证，不下载
downctl status [DIR] [--json]         # 未完成 / 失败清单
downctl verify [DIR] [--json] [--checksums FILE] [--repair]
                                      # 离线证明已完成文件（退出码 0/1/2）
downctl clean [DIR] [--dry-run] [--json]  # 清理残留位图 / 失败清单 / 锁目录
downctl plugin list|info <name>       # 插件管理
downctl config init|show|path         # 配置管理
downctl doctor                        # 环境诊断（代理/WSL/依赖/插件）
downctl wizard                        # 交互引导
downctl completions bash|zsh|fish     # 输出补全脚本
downctl version
```

示例：

```bash
downctl get https://example.com/big.iso -d ~/Downloads -t 5M
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2
```

---

## 7. 配置

路径：`~/.config/downctl/config.toml`（可用 `DOWNCTL_CONFIG` 覆盖）。
支持环境变量插值 `${VAR}`。

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 10
# max_time = 0            # 单目标墙钟上限（0 = 不限）
# probe_jobs = 8          # 解析/探测并发（0 = 跟随 jobs）
# require_witness = false # 拒绝跳过/续传版本无法核对的状态

[plugins.huggingface]
token = "${HF_TOKEN}"
mirror = "https://hf-mirror.com"

[plugins.tqdm]
enabled = true
```

---

## 8. 打包与发布

- **构建后端**：hatchling（PEP 621）
- **依赖**：`aiohttp>=3.11`、`tqdm`；`requires-python = ">=3.11"`
- **入口**：`[project.scripts] downctl = "downctl.cli:main"`
- **内置插件**：同样声明为 entry-points（便于被覆盖/替换）；`downctl.sinks` 组已于
  0.1.4 退役，加载器会对仍声明它的包给出警告而不是静默忽略
- **PyPI 发布**：GitHub Actions + `pypa/gh-action-pypi-publish`，
  使用 **Trusted Publishing（OIDC，免 API token）**，绑定
  `ThzxxArt/downctl`；`push tag v*` 触发。

---

## 9. CI/CD

- `ci.yml`：lint（ruff/mypy）+ 矩阵测试 `ubuntu/macos/windows × py3.11/3.12/3.13/3.14`
  （`pytest --cov --cov-fail-under=85`）+ `build`（构建并 `twine check`）+
  `audit`（`pip-audit` 依赖漏洞扫描）。
- **分模块覆盖率门槛**：`core/pieces.py`、`core/transfer.py`、`core/engine.py` 各自必须
  ≥95%——健康的总覆盖率会掩盖“文件算不算完成”这条状态机。
- **能证伪承诺的测试**：套件会真正启动 `python -m downctl` 子进程（`SIGTERM` 以 130 退出
  且保留进度、`kill -9` 后重启只补缺失分片：
  `tests/integration/test_cli_process.py::test_sigterm_exits_130_and_keeps_resumable_progress`、
  `tests/integration/test_cli_process.py::test_hard_kill_then_restart_resumes_only_missing_pieces`），
  并用“最终文件的 SHA-256 是否等于服务器当前 revision”做内容不变式，而不是看分片标志位
  或退出码。
- `release.yml`：tag `v*` 触发，构建并发布到 PyPI，生成 GitHub Release。
- `docs.yml`：构建 mkdocs 站点并部署到 GitHub Pages（Actions 部署，无 gh-pages 分支）。
- `codeql.yml`：安全扫描。

---

## 10. 分阶段路线图

1. **P1 骨架**：仓库 + `pyproject.toml` + 包结构 + 空 CI。
2. **P2 内核**：task/pieces/transfer/watchdog/ratelimit/locks + 单元测试。
3. **P3 插件框架**：Protocol + registry + loader + hooks + 发现测试。
4. **P4 内置插件**：url/http/sha256/filesystem/tqdm/mirror + huggingface 源。
5. **P5 CLI/配置**：子命令、TOML 配置、doctor/wizard。
6. **P6 文档**：中英 README + 插件开发指南。
7. **P7 发布**：PyPI（v0.1.0 已发布）。
8. **P8 扩展（可选）**：SFTP 细化、rich reporter、Homebrew、独立可执行文件。
9. **v0.1.2 打磨**：续传感知进度、位图可重切、`--force/--skip-existing`、
   标准输入、`--piece-size/--min-split-size`、`probe --json`、`completions`、
   `exec/checksum` 钩子、GitHub 私有资源、更丰富的 `-v` 输出。
10. **v0.1.3 状态可信**：位图绑定 URL 身份（v4）与完成标记、周期性持久化
    检查点、证据驱动的 `--skip-existing`、按原始目标重解析的 `--retry-failed`、
    日志/JSON/webhook/exec 统一脱敏、JSON 与人类输出分离、本地插件默认不加载、
    Python 3.14 CI。
11. **v0.1.4 内容可信**：位图与完成标记记录对象版本见证（纯增量字段——`BITMAP_VERSION`
    仍是 `4`、`COMPLETION_VERSION` 仍是 `2`，因此降级回 0.1.3 依然安全）、条件区间请求
    （`If-Range`）与 `Content-Range` 校验、离线证明/修复命令 `downctl verify`、
    `--require-witness`/`--recheck`、探测重试与错误分类（遵守 `Retry-After`）、
    默认有限重试、`--max-time`、`--probe-jobs`、按文件系统的整批磁盘预算、
    `sink` 类别退役、`pieces._fsync_dir` 持久化缺陷修复。

---

## 11. 风险与对策

1. **插件抽象过早固化**：API v1 发布后难以更改。对策——P3 阶段用 2-3 个真实
   源（url / hf / 本地）打磨协议后再冻结。
2. **性能开销**：每文件过插件链。对策——热路径（分片循环）只调内核，
   不穿越插件。
3. **加载安全**：本地目录插件等价于任意代码执行。对策——README 明确警告，
   默认只自动信任 entry-points 插件。
4. **测试成本高**：内核状态机 bug 最多（续传/位图/看门狗）。对策——P2
   测试先行，覆盖断线、Range、乱序续传、看门狗等分支。
