# 插件开发

downctl 围绕一个理念构建：**一切皆插件**。有状态的内核（调度、分片位图、卡死看门狗、
限速、锁、磁盘检查）保持稳定；其余所有策略——源、传输、校验、进度、路由、钩子
——都是插件，内置实现也不例外。

## 插件类别

| 类别 | entry-point group | 职责 | 内置实现 |
|---|---|---|---|
| Source | `downctl.sources` | 把输入解析成下载任务 | `url`、`huggingface`、`github`、`s3`、`sftp`、`file` |
| Transport | `downctl.transports` | 传输数据 | `http`、`sftp`、`file` |
| Verifier | `downctl.verifiers` | 校验摘要 | `sha256`、`sha1`、`sha512`、`md5` |
| Reporter | `downctl.reporters` | 进度与日志 | `tqdm`、`json`、`quiet` |
| Router | `downctl.routers` | URL 改写 | `mirror` |
| Hook | `downctl.hooks` | 生命周期事件 | `logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum` |

> `sink` 类别已于 0.1.4 退役：它从未被写入路径调用（内核一直通过 `core/transfer.py`
> 直接写入），承诺了可替换的落地方式却从未生效。仍声明 `downctl.sinks` 的安装包会在
> 加载时收到警告，`downctl plugin list` 也不再列出 `sink`——`API_VERSION` 仍为 `"1"`，
> 其余六类插件不受影响。

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

## 发现机制

插件从三处发现，优先级递增（后加载者可按名称覆盖前者）：

1. **内置**（`downctl.builtin`）
2. **已安装包**声明的 `downctl.*` entry-point
3. **本地目录**——`./downctl_plugins` 与 `~/.config/downctl/plugins`
   （需 `--local-plugins` 显式启用）

### 打包为插件

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

安装后（`pip install .`）downctl 会自动识别。

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
