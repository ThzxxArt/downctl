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
| Sink | 数据落地 |
| Reporter | 进度与日志呈现 |
| Router | URL 改写 / 镜像切换 |
| Hook | 生命周期事件回调 |

### 2.2 内核层（稳定、不插件化）

以下能力是**有状态的紧耦合状态机**，拆成插件只会带来性能损耗与脆弱性，
因此作为稳定的 **Core API** 暴露给插件调用，本身不是插件：

- 分片调度与并发控制
- 断点续传位图（piece bitmap）
- 卡死看门狗（stall watchdog）
- 限速（rate limit）
- 单文件 / 单实例锁
- 磁盘空间预检

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
│   ├── platform_utils.py        # WSL/VT 颜色/安全 IO/pid 检活
│   ├── plugin/
│   │   ├── __init__.py
│   │   ├── protocols.py         # 七类插件 Protocol 定义
│   │   ├── registry.py          # 注册表 + 选择/优先级
│   │   ├── loader.py            # entry_points + 本地目录发现
│   │   └── hooks.py             # 生命周期事件总线
│   ├── core/                    # ★ 稳定内核（不插件化）
│   │   ├── __init__.py
│   │   ├── engine.py            # 调度 + 多次重试编排
│   │   ├── transfer.py          # 分片下载
│   │   ├── pieces.py            # 分片位图
│   │   ├── watchdog.py          # StallTimeout
│   │   ├── ratelimit.py         # 令牌桶限速
│   │   ├── locks.py             # 目录锁
│   │   ├── task.py              # DownloadTask 数据模型
│   │   ├── context.py           # RuntimeContext（注入 session/配置/事件）
│   │   └── errors.py            # 异常定义
│   └── builtin/                 # 内置插件（全部走插件接口）
│       ├── __init__.py
│       ├── sources/{url_source.py, huggingface_source.py}
│       ├── transports/{http_transport.py}
│       ├── verifiers/{sha256_verifier.py}
│       ├── sinks/{filesystem_sink.py}
│       ├── reporters/{tqdm_reporter.py, json_reporter.py, quiet_reporter.py}
│       ├── routers/{mirror_router.py}
│       └── hooks/{logfile_hook.py}
├── tests/{unit/, integration/}
├── docs/
├── examples/plugins/            # 第三方插件示例
├── .github/workflows/{ci.yml, release.yml, codeql.yml}
├── pyproject.toml
├── README.md / README.en.md
├── CHANGELOG.md / CONTRIBUTING.md / SECURITY.md / CODE_OF_CONDUCT.md
└── LICENSE
```

---

## 4. 插件体系

### 4.1 七类插件

| 类型 | entry-point group | 内置实现 |
|---|---|---|
| Source | `downctl.sources` | `url`、`huggingface`、`github`、`s3`、`sftp` |
| Transport | `downctl.transports` | `http`、`sftp` |
| Verifier | `downctl.verifiers` | `sha256` |
| Sink | `downctl.sinks` | `filesystem` |
| Reporter | `downctl.reporters` | `tqdm`、`json`、`quiet` |
| Router | `downctl.routers` | `mirror` |
| Hook | `downctl.hooks` | `logfile`、`archive`、`webhook`、`manifest` |

### 4.2 发现机制（优先级从高到低）

1. **本地目录**：`./downctl_plugins/` 与 `~/.config/downctl/plugins/`
2. **pip 包**：通过 `importlib.metadata` 读取 entry-points
3. **内置**：`downctl.builtin`

### 4.3 选择机制

- 按 **URI scheme 路由**：`https://`→`http`，`hf://org/repo`→`huggingface`，
  未来 `s3://`、`gh://` 等。
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

## 5. CLI 设计

```
downctl get <target> [<target>...]    # 下载，按 scheme 自动路由源插件
    -d/--dir            保存目录
    -o/--output         保存为指定完整路径（单文件）
    -j/--jobs           并发文件数
    -t/--limit-rate     限速，如 5M / 1m / 512k
    -r/--retries        最大重试次数（0=无限）
    --checksum          sha256:<hex>
    --proxy             代理地址
    --mirror FROM=TO    镜像改写
    --source <name>     强制指定源插件
    --reporter <name>   指定进度呈现插件
    --dry-run           只探测不下载
    --retry-failed      仅重试上次失败项

downctl probe <target>                # 只探测大小/状态，不下载
downctl plugin list|info <name>       # 插件管理
downctl config init|show|path         # 配置管理
downctl doctor                        # 环境诊断（代理/WSL/依赖/磁盘）
downctl wizard                        # 交互引导
downctl version
```

示例：

```bash
downctl get https://example.com/big.iso -d ~/Downloads -t 5M
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2
```

---

## 6. 配置

路径：`~/.config/downctl/config.toml`（可用 `DOWNCTL_CONFIG` 覆盖）。
支持环境变量插值 `${VAR}`。

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 0

[plugins.huggingface]
token = "${HF_TOKEN}"
mirror = "https://hf-mirror.com"

[plugins.tqdm]
enabled = true
```

---

## 7. 打包与发布

- **构建后端**：hatchling（PEP 621）
- **依赖**：`aiohttp`、`tqdm`；`requires-python = ">=3.11"`
- **入口**：`[project.scripts] downctl = "downctl.cli:main"`
- **内置插件**：同样声明为 entry-points（便于被覆盖/替换）
- **PyPI 发布**：GitHub Actions + `pypa/gh-action-pypi-publish`，
  使用 **Trusted Publishing（OIDC，免 API token）**，绑定
  `ThzxxArt/downctl`；流程为 `TestPyPI 验证 → 正式 PyPI`，
  `push tag v*` 触发。

---

## 8. CI/CD

- `ci.yml`：矩阵 `ubuntu/macos/windows × py3.11/3.12/3.13`，
  执行 `ruff` + `mypy` + `pytest`。
- `release.yml`：tag 触发，构建并发布到 PyPI，生成 GitHub Release。
- `codeql.yml`：安全扫描。

---

## 9. 分阶段路线图

1. **P1 骨架**：仓库 + `pyproject.toml` + 包结构 + 空 CI。
2. **P2 内核**：task/pieces/transfer/watchdog/ratelimit/locks + 单元测试。
3. **P3 插件框架**：Protocol + registry + loader + hooks + 发现测试。
4. **P4 内置插件**：url/http/sha256/filesystem/tqdm/mirror + huggingface 源。
5. **P5 CLI/配置**：子命令、TOML 配置、doctor/wizard。
6. **P6 文档**：中英 README + 插件开发指南。
7. **P7 发布**：TestPyPI → PyPI → tag v1.0.0。
8. **P8 扩展（可选）**：S3/GitHub Release 源、rich reporter、Docker、Homebrew。

---

## 10. 风险与对策

1. **插件抽象过早固化**：API v1 发布后难以更改。对策——P3 阶段用 2-3 个真实
   源（url / hf / 本地）打磨协议后再冻结。
2. **性能开销**：每文件过插件链。对策——热路径（分片循环）只调内核，
   不穿越插件。
3. **加载安全**：本地目录插件等价于任意代码执行。对策——README 明确警告，
   默认只自动信任 entry-points 插件。
4. **测试成本高**：内核状态机 bug 最多（续传/位图/看门狗）。对策——P2
   测试先行，覆盖断线、Range、乱序续传、看门狗等分支。
