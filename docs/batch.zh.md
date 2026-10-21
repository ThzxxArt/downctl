# 作业、凭据与交付

`downctl` 0.1.6 把生命周期的两端也接入了插件体系：

- **作业**——一个文件描述多组目标，每组有自己的目录与选项（`downctl batch`）；
- **凭据**——由**主机**而不是某个源插件决定材料从哪来（`[auth."<host>"]` 与
  `downctl auth check`）；
- **交付**——把下载好的目录以 downctl 自己说的 Range 语义交给另一台机器
  （`downctl serve`）。

`plugin check` 与 `plugin new` 让第三方作者可以验证这些契约。全部是加性变更：
`API_VERSION` 仍为 `"1"`，状态格式不变。

## 批量作业文件

```bash
downctl batch jobs.toml
downctl batch jobs.toml --job llama --job tools
downctl batch jobs.json --dry-run
```

`<文件>` 是 TOML（`.toml`/`.tml`）或 JSON（`.json`），由能识别该扩展名的 `jobsource`
插件读取（`--job-source NAME` 可强制指定）。两种格式描述同一套 schema：

```toml
version = 1

[[job]]
name = "llama"
targets = ["hf://meta-llama/Llama-3-8B@main"]
dir = "models/llama"
include = ["*.safetensors", "*.json"]
checksums = "checksums.txt"

[[job]]
name = "tool"
targets = ["https://example.com/tool.tar.gz"]
checksum = "sha256:9f2c..."
hook = ["archive"]
```

同一份文档的 JSON 写法：

```json
{
  "version": 1,
  "jobs": [
    {
      "name": "llama",
      "targets": ["hf://meta-llama/Llama-3-8B@main"],
      "dir": "models/llama",
      "include": ["*.safetensors", "*.json"],
      "checksums": "checksums.txt"
    }
  ]
}
```

### 一个作业可以写什么

| 键 | 必填 | 含义 |
|---|---|---|
| `name` | 否 | 稳定标识（省略时为 `job-<序号>`）；重名会被拒绝 |
| `targets` | 是 | 一个目标字符串，或非空字符串数组，语法与 `downctl get` 相同 |
| *(其余键)* | 否 | `get` 的选项，写成下划线形式 |

`downctl get` 的**下载选项**都可以用，**除了**那些描述批次本身的键。以下键会被拒绝
并说明原因：

| 被拒绝的键 | 原因 |
|---|---|
| `list` | 批次文件本身就是目标清单 |
| `retry_failed` | 失败清单是整次运行的，不是单个作业的 |
| `reporter` | 一次运行只有一个报告器 |
| `local_plugins` | 本地插件目录是进程级的 |

别名 `checksum_manifest` → `checksums`、`hooks` → `hook`、`headers` → `header`、
`continue` → `continue_` 可用；同一选项用两种拼写给出会报错。`version` 必填且必须为
`1`；更大的数字会以“由更新版本的 downctl 写入”为由拒绝。相对的 `dir` 与
`checksums` 按作业文件所在目录解析，因此文件可以整体搬移。类型会被检查而不是强转：
`force = "false"` 是错误，不是真值字符串。

`downctl batch` 自己的选项是每个作业的默认值；作业只覆盖它写出的键。`output` 是
作业级选项，且仅当该作业恰好只有一个目标时允许（`batch` 命令本身没有 `-o`）。

### 批次如何执行

作业按文件顺序**串行**执行，共享一个 session、一个注册表、一个事件总线与一份失败清单。
每个作业都会在**第一个下载开始之前**完成解析与校验（包括校验和清单），因此最后一个作业
写错时，不会等到第一个作业已经写入字节才报错。

- `--job NAME`（可多次）只运行指定作业；未知名字是错误。
- `--continue` 在一个作业失败后继续运行后面的作业；不加则停在第一个失败的作业，并报告
  有多少作业未开始。
- `--dry-run` 探测所有作业但不下载；作业内的 `dry_run` 同样有效。
- `on_start`/`on_finish` 载荷带 `"job": <名称>`，JSON 报告器的 `run_start`/`run_finish`
  记录也带同一个字段。

退出码：

| 码 | 含义 |
|---|---|
| `0` | 选中的作业全部成功 |
| `1` | 有目标或作业失败（可能已写入字节） |
| `2` | 作业文件非法：解析/校验失败，一个字节都没下 |

运行中的失败（例如不带 `--continue` 的探测 `404`）是退出码 `1`，不是 `2`；`2` 只留给
根本无法变成作业的文档。

## 凭据

凭据按**主机**配置，而不是按源插件：

```toml
[auth."huggingface.co"]
provider = "env"
env = "HF_TOKEN"
header = "Authorization"
prefix = "Bearer "

[auth."*.corp.example"]
provider = "netrc"

[auth."private.example.com"]
provider = "command"
command = "pass show example/token"
ttl = 300
```

### 匹配规则

1. 精确主机优先（`huggingface.co`）。
2. 否则最长后缀的 `*.suffix` 胜出。通配符**只匹配子域**：`*.corp.example` 匹配
   `git.corp.example`，不匹配 `corp.example`。
3. 都不匹配时不附加任何 provider 材料。

同一主机定义两次是错误，而不是“后写的赢”：两张表不能同时是同一台主机的答案。

### Provider

| Provider | 选项 | 行为 |
|---|---|---|
| `env` | `env`（名称或列表）、`header`、`prefix` | 第一个非空环境变量胜出；全部未设置是错误 |
| `command` | `command`、`timeout`（默认 10 秒）、`header`、`prefix` | 运行命令，取**唯一**一条非空 stdout 行作为令牌。非零退出或多于一行都是错误 |
| `netrc` | `path`（默认 `~/.netrc`） | 为匹配的 `machine` 返回 HTTP Basic；文件不存在表示“没有材料” |

`header` 默认 `Authorization`，`prefix` 默认 `Bearer `（允许空前缀）。Provider 只回答
“材料是什么”，**绝不**决定“谁可以收到它”：那仍由 `policy_request` 的逐源规则负责。
材料只为请求的第一跳源解析一次，重定向离开该源即丢弃。显式
`-H Authorization: ...` 或 `-u USER:PASS` 优先于 provider。

`ttl` 单位为秒，默认 `0`，表示整次运行复用同一份材料。会签发短期令牌的 `command`
provider 可以设 `ttl = 300` 定期续取。

### 检查解析结果

```bash
downctl auth check https://huggingface.co/meta-llama/Llama-3-8B
downctl auth check --json https://private.example.com/file.bin
```

文本输出会给出主机、命中的 `[auth."<host>"]` 规则、provider，以及材料的**名字**
（绝不是值）。`--json` 为每个 URL 输出一个对象，含 `url`、`host`、`rule`、`provider`、
`provider_kind`、`material`（`headers`/`basic`/`source`）与 `error`。provider 失败时
命令退出 `1`；错误只点名规则与 provider，绝不包含值。`downctl doctor` 会列出已配置的
规则，以及每条规则指定的 provider 是否存在。

## 提供下载服务

```bash
downctl serve ~/models --bind 127.0.0.1 --port 8080
downctl serve ./model.bin --auth alice:secret --index --quiet-logs
```

| 选项 | 默认 | 含义 |
|---|---|---|
| `--bind ADDR` | `127.0.0.1` | 监听地址；非回环地址会打印警告 |
| `--port N` | `8080` | 监听端口 |
| `--auth USER:PASS` | 无 | 要求 HTTP Basic 凭据 |
| `--index` | 关闭 | 渲染 HTML 目录列表；关闭时目录请求返回 `404` |
| `--quiet-logs` | 关闭 | 不记录请求 |

目录按其根路径提供；单个文件在 `/` 及其文件名下提供。HTTP 子集正是下载器需要的：

- `GET`/`HEAD`；单个 `Range` 返回 `206` + `Content-Range`，无法满足时返回 `416` +
  `Content-Range: bytes */<大小>`；多区间被忽略并以 `200` 返回完整内容。
- `If-Range` 与当前 `ETag`/`Last-Modified` 比对。
- `ETag` 优先取 `.downctl-done` 中记录的 validator，否则用
  `W/"<大小>-<mtime_ns>"`；`Last-Modified` 是文件 mtime。
- Basic 认证在解析路径**之前**检查，错误凭据无法得知哪些文件存在。
- 路径先百分号解码再解析，并与真实根目录比对；`..` 与指向根外的符号链接返回 `403`。
- 请求日志不包含 query string。

用 `Ctrl+C` 停止（POSIX 与可用平台上的 `SIGINT`/`SIGTERM`）。

### 一次往返

```bash
# 机器 A
downctl serve ~/models --port 8080

# 机器 B
downctl get http://127.0.0.1:8080/llama/model.safetensors -d ./copy
downctl verify ./copy
```

服务端从完成标记派生 `ETag`，因此下载端看到的就是当初验证过的那个 validator，
交接后的续传与 `If-Range` 仍然成立。绑定非回环地址时，请加 `--auth`，或把服务放在
你信任的代理后面。

## 编写与检查插件

```bash
downctl plugin new example --kind source --dir .
pip install -e example_plugin
downctl plugin check example_plugin.plugin
downctl plugin check example_plugin          # 也可以给 distribution 名
```

`plugin new` 会写出一个能直接工作的包——带正确 entry-point 组的 `pyproject.toml`、
插件类、测试与 README；生成的测试在你改任何代码之前就能通过。`--kind` 取
`source`、`transport`、`verifier`、`reporter`、`router`、`hook`、`jobsource`、
`auth`、`server`。

`plugin check` 加载模块（`pkg.module` 或 `pkg.module:Attr`）或已安装的 distribution，
运行注册表的结构检查，并报告检测到的可选能力。任何问题都会以退出码 `1` 结束，并点名
缺了什么。同一套检查也以公共模块 `downctl.plugin.testing` 提供给插件作者：

```python
from downctl.plugin.registry import Registry
from downctl.plugin.testing import assert_registers, check_plugin, fake_context

from example_plugin.plugin import ExampleSource


def test_contract() -> None:
    plugin = ExampleSource()
    assert check_plugin(plugin, "source") == []
    assert_registers(Registry(), "source", plugin)
```

`check_plugin`、`capabilities`、`option_names`、`assert_registers` 与
`fake_context` 都是插件 API 的一部分。

## 另见

- [命令行参考](cli.md) —— 全部命令与退出码
- [配置](configuration.md) —— `[auth]` 与 `[core]`、`[plugins]` 的关系
- [插件开发](plugins.md) —— 九类插件与打包
- [实战配方](cookbook.md) —— 更多端到端示例
