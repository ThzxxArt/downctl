# 插件开发

downctl 围绕一个理念构建：**一切皆插件**。有状态的内核（调度、分片位图、卡死看门狗、
限速、锁、磁盘检查）保持稳定；其余所有策略——源、传输、校验、进度、路由、钩子、
作业源、凭据提供者、交付服务——都是插件，内置实现也不例外。

## 插件类别

| 类别 | entry-point group | 职责 | 内置实现 |
|---|---|---|---|
| Source | `downctl.sources` | 把输入解析成下载任务 | `url`、`http_index`、`huggingface`、`github`、`s3`、`sftp`、`file` |
| Transport | `downctl.transports` | 传输数据 | `http`、`sftp`、`file` |
| Verifier | `downctl.verifiers` | 校验摘要 | `sha256`、`sha1`、`sha512`、`md5` |
| Reporter | `downctl.reporters` | 进度与日志 | `live`、`plain`、`tqdm`、`json`、`quiet` |
| Router | `downctl.routers` | URL 改写 | `mirror` |
| Hook | `downctl.hooks` | 生命周期事件 | `logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum`、`metrics`、`notify` |
| Job source | `downctl.jobsources` | 读取批量作业文件 | `toml`、`json` |
| Auth provider | `downctl.auths` | 为主机提供凭据 | `env`、`command`、`netrc` |
| Server | `downctl.servers` | 把文件交给另一台机器 | `http` |

> `sink` 类别已于 0.1.4 退役：它从未被写入路径调用（内核一直通过 `core/transfer.py`
> 直接写入），承诺了可替换的落地方式却从未生效。仍声明 `downctl.sinks` 的安装包会在
> 加载时收到警告。`API_VERSION` 仍为 `"1"`，其余类别插件不受影响。

内置插件**只通过代码注册**，不再出现在 `metadata.entry_points` 里：安装了 downctl 的
环境不会把它自己当成插件包。被扫描的只有上表九个组；仅仅以 `downctl.` 开头的组不是
插件发现通道。

## 契约

插件是任意对象，只需暴露元数据（`name`、`api_version`、`priority`）以及
`downctl.plugin.protocols` 中某个 Protocol 的方法。它是**结构化**的，无需导入
downctl 即可被发现；但推荐用这些 Protocol 做类型标注。

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

`ctx` 是 `downctl.core.context.RuntimeContext`，提供共享的 `aiohttp` session、
生效配置、注册表、事件总线，以及单次运行的 `params`。

### 编写 reporter

reporter 是观察者，不能让下载失败。只有三个必选方法，其余全部可选、按存在性探测：

```python
class ExampleReporter:
    name = "example"
    api_version = API_VERSION
    priority = 0

    def task_started(self, task): ...
    def task_progress(self, task, delta): ...   # delta 是本次尝试的增量
    def task_finished(self, task, ok): ...
```

`run_started`/`run_finished`/`run_resumed`/`task_resumed`/`failure` 是 0.1.6
的可选扩展。0.1.7 新增 `phase(name, done, total)`、
`task_phase(task, phase, detail)`、`task_retrying(task, attempt, delay, reason)`、
`task_restarted(task)`；四者都只用于呈现，经
`downctl.plugin.callbacks.call_optional_safe` 调用，抛异常只会停用显示并警告一次，
不会让文件失败。新关键字（`job`、`unknown`、`connections`）只在你声明了形参或
接受 `**kwargs` 时传入。只有 stdout 完全归你时才设 `machine_readable = True`；
console 会据此静音人类输出。可实现 `configure(options)` 接收 `[plugins.<名称>]` 表。

内置 `live` reporter 是完整参考：状态在 `downctl.progress.model`，纯渲染在
`downctl.progress.render`，唯一持有流与光标的是 `downctl.progress.terminal`。

## 发现机制

插件从三处发现，优先级递增（后加载者可按名称覆盖前者，并给出警告）：

1. **内置**（`downctl.builtin`）
2. **已安装包**声明的九个 `downctl.*` 组之一
3. **本地目录**——`./downctl_plugins` 与 `$XDG_CONFIG_HOME/downctl/plugins`
   （未设置时为 `~/.config/downctl/plugins`；需 `--local-plugins` 显式启用）

入口点可以加载类、实例或零参工厂函数。插件的 `name` 属性才是注册表的键，
entry-point 名只是标签。同名注册会以警告替换前一个实现；`downctl plugin info NAME`
会显示 `origin`、`api_version`、`options` 以及（存在时的）`capabilities`，因此能看出
哪一个赢了。

内置**源**以 `priority = 10` 认领各自的 scheme（`url` 为 `0`，`http_index` 为 `5`），
所以第三方源要覆盖同一目标，必须使用**更高**的 priority；`--source NAME` 仍可显式强制。

### 打包为插件

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

值也可以是工厂：

```toml
[project.entry-points."downctl.jobsources"]
myjobs = "my_package.jobs:make_source"
```

安装后（`pip install .`）downctl 会自动识别。想从可工作的骨架开始而不是空文件：

```bash
downctl plugin new example --kind source --dir .
pip install -e example_plugin
downctl plugin check example_plugin.plugin
```

`downctl plugin check` 也接受模块（`pkg.module`、`pkg.module:Attr`）或已安装的
distribution 名；任何契约问题都以 `1` 退出。同一套检查也可从
`downctl.plugin.testing` 导入（`check_plugin`、`capabilities`、`option_names`、
`assert_registers`、`fake_context`）。

### 启用插件

源、进度、路由会被自动选择或通过 CLI 指定（`--source`、`--reporter`、`--mirror`）。
钩子是**可选**的：用 `--hook NAME` 单次启用，或在配置里常驻启用；其选项来自
`[plugins.<name>]`。

```toml
[core]
hooks = ["logfile"]

[plugins.logfile]
path = "~/.cache/downctl/run.log"
```

`archive` 钩子会在下载完成后解压 zip/tar：

```toml
[core]
hooks = ["archive"]

[plugins.archive]
into = "~/Downloads/extracted"   # 可选；默认解压到同名兄弟目录
keep = false                     # 解压后删除原压缩包
```

`notify` 钩子把一行运行摘要发到通用 webhook，或发到 [ntfy](https://ntfy.sh/)
主题（设置 `topic` 时）：

```toml
[core]
hooks = ["notify"]

[plugins.notify]
# 通用 webhook：收到 {"title", "message", "url"}。
url = "https://example.com/hooks/downctl"
title = "nightly models"
```

```toml
[plugins.notify]
# ntfy：向服务器根地址 POST {"topic", "message", "title"}。
url = "https://ntfy.sh"
topic = "my-downctl"
only_failures = true
```

`url` 与 `command` 只能设置一个；命令运行超过 `timeout`（默认 `10.0` 秒）会连同
进程组被杀死，并记为钩子失败。

`webhook` 钩子把选定事件以 JSON POST，`manifest` 钩子为已完成下载写 `sha256sum` 清单
（可日后配合 `--checksums` 复验）：

```toml
[core]
hooks = ["webhook", "manifest"]

[plugins.webhook]
url = "https://example.com/downctl"
events = ["on_finish"]

[plugins.manifest]
filename = ".downctl.manifest"
```

`checksum` 与 `manifest` 是**证据**钩子：`on_task_finish` 报告 `witness = none`
（这次完成无法与任何对象版本绑定）时，它们**不写边车、也不写清单条目**。这两种产物都会
被 `--checksums` 读回去；为一份未证明的文件写产物，是一个没人兑现过的承诺。

新类别的选择方式不同：`jobsource` 按批量文件扩展名选择（`--job-source` 可覆盖），
`auth` provider 由 `[auth."<host>"] provider = "..."` 指定，`downctl serve` 使用的
`server` 按名字查找（目前是 `http`）。它们的协议方法都很小：

```python
from downctl.core.credentials import AuthMaterial


class ExampleAuth:
    name = "example"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, host: str) -> bool:
        return host.endswith("example.com")

    async def material(self, host, ctx, options):
        return AuthMaterial(headers={"Authorization": "Bearer ..."}, source="example")
```

### 本地插件

在 `./downctl_plugins/` 放一个模块，定义 `register(registry)` 函数：

```python
def register(registry):
    registry.register("source", ExampleSource(), origin="example")
```

> **安全提示**：插件即任意代码执行。只安装或启用你信任的包/目录；正因如此
> `--local-plugins` 需要显式开启。

## API 版本化

插件契约由 `downctl.API_VERSION`（当前 `"1"`）版本化。插件声明其构建时的版本；
注册表拒绝不匹配者。对契约的破坏性变更会提升 API 版本。

## 另见

- [`design.md`](design.md) —— 架构与内核/插件边界
- [`examples/plugins/`](https://github.com/ThzxxArt/downctl/tree/master/examples/plugins)
  —— 最小可安装插件示例
