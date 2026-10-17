# 钩子

钩子用于观察下载生命周期。它们是**可选**的：用 `--hook NAME`（可多次）单次启用，
或在 `[core] hooks = [...]` 常驻启用；其选项位于 `[plugins.<名称>]`。

## 事件

| 事件 | 载荷 |
|---|---|
| `on_start` | `{targets: [...]}` |
| `on_task_start` | `{task}` |
| `on_task_finish` | `{task, ok}` |
| `on_error` | `{task, error}` |
| `on_finish` | `{succeeded, failed}` |

内置钩子：`logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum`。

---

## `logfile`

把每个事件按 TSV 追加写入文件。

| 键 | 默认 | 含义 |
|---|---|---|
| `path` | `downctl.log` | 日志文件（支持 `~` 展开） |

```toml
[core]
hooks = ["logfile"]

[plugins.logfile]
path = "~/.cache/downctl/run.log"
```

---

## `archive`

任务完成时解压下载好的压缩包；对路径穿越做了防护，并拒绝链接/设备/FIFO。

| 键 | 默认 | 含义 |
|---|---|---|
| `into` | 同名兄弟目录 | 解压目标目录 |
| `keep` | `true` | 解压后保留原压缩包 |

支持：`.zip`、`.tar`、`.tar.gz`/`.tgz`、`.tar.bz2`/`.tbz2`、`.tar.xz`/`.txz`。

```toml
[core]
hooks = ["archive"]

[plugins.archive]
into = "~/Downloads/extracted"
keep = false
```

---

## `webhook`

把选定事件以 JSON POST 出去（fire-and-forget，退出前会 flush）。

| 键 | 默认 | 含义 |
|---|---|---|
| `url` | 无 | 端点（必填） |
| `events` | `["on_finish"]` | 要发送的事件 |
| `timeout` | `30` | 请求超时（秒） |

```toml
[core]
hooks = ["webhook"]

[plugins.webhook]
url = "https://example.com/downctl"
events = ["on_start", "on_task_finish", "on_finish"]
```

任务对象序列化为 `{url, target, source}`。

---

## `manifest`

为已完成的下载写一份 `sha256sum` 风格清单，可日后配合 `--checksums` 复验。

| 键 | 默认 | 含义 |
|---|---|---|
| `filename` | `.downctl.manifest` | 清单文件名 |

```toml
[core]
hooks = ["manifest"]

[plugins.manifest]
filename = ".downctl.manifest"
```

用上一次运行的清单复验一次重下：

```bash
downctl get <url> -d new --checksums old/.downctl.manifest
```

---

## `exec`

事件触发时执行 shell 命令。

| 键 | 默认 | 含义 |
|---|---|---|
| `command` | 无 | shell 命令（必填） |
| `events` | `["on_task_finish"]` | 触发的命令 |
| `timeout` | `300` | 命令被杀前的秒数 |

事件信息通过环境变量传入：`DOWNCTL_EVENT`、`DOWNCTL_TARGET`、`DOWNCTL_PATH`、
`DOWNCTL_URL`、`DOWNCTL_OK`。

```toml
[core]
hooks = ["exec"]

[plugins.exec]
command = "echo \"$DOWNCTL_TARGET -> $DOWNCTL_PATH\" >> ~/downloads.log"
```

> **安全**：命令以你的权限运行，只配置你信任的命令。

---

## `checksum`

为每个已完成的下载写一份 `<文件>.<算法>` 边车，格式为 `sha256sum` 行
（`<hex>  <文件名>`），可日后配合 `--checksums` 使用。

| 键 | 默认 | 含义 |
|---|---|---|
| `algo` | `sha256` | 哈希算法（`sha256`/`sha1`/`sha512`/`md5`） |
| `events` | `["on_task_finish"]` | 触发的命令 |

```toml
[core]
hooks = ["checksum"]

[plugins.checksum]
algo = "sha256"
```
