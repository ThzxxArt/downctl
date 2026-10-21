# 钩子

钩子用于观察下载生命周期。它们是**可选**的：用 `--hook NAME`（可多次）单次启用，
或在 `[core] hooks = [...]` 常驻启用；其选项位于 `[plugins.<名称>]`。

## 事件

| 事件 | 载荷 |
|---|---|
| `on_start` | `{targets: [...]}`（批处理中额外带 `job`） |
| `on_task_start` | `{task}` |
| `on_task_finish` | `{task, ok, witness}`；跳过的任务额外带 `skipped: true` |
| `on_error` | `{task, error}` |
| `on_finish` | `{succeeded, failed, hooks_failed, bytes, elapsed}`（批处理中额外带 `job`） |

`hooks_failed` 是本次运行中钩子失败的次数。它在等待在途的钩子工作排空之后才统计，
因此是最终值，而不是在 checksum/exec/webhook 线程汇报之前拍下的快照。

内置钩子：`logfile`、`archive`、`webhook`、`manifest`、`exec`、`checksum`、
`metrics`、`notify`。

`witness` 取值为 `validator`（对象的版本见证一致）、`checksum`（给出的摘要复验通过）或
`none`（完全无法核对对象版本，该次跳过会提示 unversioned）。它只在任务成功结束时出现；
失败通过 `on_error` 报告。`witness = none` 时 `checksum` 与 `manifest` **不写任何产物**：
两者都会被 `--checksums` 读回去，为一份从未与版本绑定的文件写产物，是一个没人兑现过的承诺。

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

任务完成时解压下载好的压缩包；对路径穿越做了防护。tar 中的链接/设备/FIFO 会被拒绝；
zip 只做同样的路径校验（Python 的 `zipfile` 会把成员写成普通文件，不存在 tar 式的链接/
设备类型）。声明的展开量超过上限时，在解压之前就拒绝。被跳过的任务（`skipped: true`）
不会再次解压：真正产出它的那次运行已经解压过了。

| 键 | 默认 | 含义 |
|---|---|---|
| `into` | 同名兄弟目录 | 解压目标目录 |
| `keep` | `true` | 解压后保留原压缩包 |
| `max_entries` | `200000` | 条目数超过该值的压缩包会被拒绝（`0` = 不限） |
| `max_total_bytes` | `20 GiB` | 声明的解压后总字节超过该值会被拒绝（`0` = 不限） |

声明的展开比超过压缩体积 `1000x` 也会被拒绝（`.zip` 与 `.tar.*` 都适用）。

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

任务对象序列化为 `{url, target, source, origin}`。`url` 与 `origin` 均已脱敏
（预签名 query 被剥离），webhook 端点不会收到 S3 签名或 URL 里的令牌。

---

## `manifest`

为已完成的下载写一份 `sha256sum` 风格清单，可日后配合 `--checksums` 复验。新条目会
**合并**进已有清单而不是覆盖它，因此对同一目录里一个文件再跑一次，不会抹掉第一次运行
已经证明过的条目。

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
| `events` | `["on_task_finish"]` | 触发该钩子的事件 |
| `timeout` | `300` | 命令被杀前的秒数 |

事件信息通过环境变量传入：`DOWNCTL_EVENT`、`DOWNCTL_TARGET`、`DOWNCTL_PATH`、
`DOWNCTL_URL`、`DOWNCTL_ORIGIN`、`DOWNCTL_OK`。`DOWNCTL_URL` 会剥离预签名
query；两者都会去掉 userinfo 并掩码敏感 query 值，钩子脚本不会误把签名写进日志。
JSON reporter 模式下，命令的 stdout 会重定向到 stderr，避免钩子输出污染 JSON 流。

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
| `events` | `["on_task_finish"]` | 触发的事件。只接受 `on_task_finish`：只有它携带边车所声称依赖的见证 |

每个受支持的算法都有内置校验器，因此任意算法写出的边车都能再通过
`--checksums FILE` 复验。发布方提供选择时优先用 `sha256`/`sha512`；
`md5`/`sha1` 仅为兼容上游校验文件。

```toml
[core]
hooks = ["checksum"]

[plugins.checksum]
algo = "sha256"
```

---

## `metrics`

在 `on_finish` 时把整次运行的统计写成 Prometheus textfile，原子替换
（`<path>.<pid>.tmp` + `os.replace`），抓取端看到的要么是上一份完整文件，要么是新文件。

| 键 | 默认 | 含义 |
|---|---|---|
| `path` | `downctl.prom` | 输出文件（支持 `~` 展开） |
| `labels` | `[]` | 附加到每条指标的 `name=value` 标签 |

```toml
[core]
hooks = ["metrics"]

[plugins.metrics]
path = "/var/lib/node_exporter/textfile/downctl.prom"
labels = ["host=build-01"]
```

指标：`downctl_run_duration_seconds`、`downctl_files_total{outcome}`、
`downctl_bytes_total`、`downctl_failures_total{reason}`、
`downctl_hook_failures_total`。写入失败会计为钩子失败，`strict_hooks` 能看到。

---

## `notify`

通过 HTTP 或命令发送一行运行摘要。

| 键 | 默认 | 含义 |
|---|---|---|
| `url` | 无 | 向 ntfy 兼容端点 POST JSON（`title`/`body`/`priority`） |
| `command` | 无 | 改为运行 shell 命令；摘要通过 `DOWNCTL_*` 变量传入 |
| `events` | `["on_finish"]` | 触发通知的事件 |
| `only_failures` | `false` | 干净结束时不发通知 |
| `title` | `downctl` | 通知标题 |

`url` 与 `command` 必须恰好设置一个。

```toml
[core]
hooks = ["notify"]

[plugins.notify]
url = "https://ntfy.sh/my-downctl"
only_failures = true
```

投递在后台任务（或子进程）中进行，慢的通知服务不会阻塞下载。命令会收到
`DOWNCTL_EVENT`、`DOWNCTL_SUCCEEDED`、`DOWNCTL_FAILED`、`DOWNCTL_BYTES` 与
`DOWNCTL_ELAPSED`。正文已脱敏；失败会被报告并计数。
