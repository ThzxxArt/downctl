# 命令行参考

```
downctl [--version] <command> [options]
```

## `downctl get`

下载一个或多个目标。HTTP(S) 链接由 `url` 源处理；`hf://org/name[@revision]`
（或裸 `org/name`）由 `huggingface` 源处理；`gh://owner/repo[@tag]` 由 `github`
源处理；`s3://bucket/key`（或 `s3://bucket/prefix/`）由 `s3` 源处理；
`sftp://[user@]host[:port]/path` 由 `sftp` 源处理（需 `downctl[sftp]`）；
`file:///path` 由 `file` 源处理。

| 选项 | 说明 |
|---|---|
| `-d, --dir DIR` | 保存目录（默认 `~/Downloads`） |
| `-o, --output PATH` | 单文件另存为指定完整路径 |
| `-j, --jobs N` | 并发文件数 |
| `--connections N` | 单文件连接数（默认 16） |
| `--stall N` | 卡死看门狗阈值秒数（默认 30） |
| `--checkpoint N` | 续传状态落盘间隔秒数（默认 30；`0` = 仅退出时） |
| `--piece-size SIZE` | 分片大小（`8M`/`4M`，默认自动） |
| `--min-split-size SIZE` | 小于该大小的文件走单连接 |
| `-t, --limit-rate RATE` | 限速（`5M`、`1m`、`512k`），**全局预算** |
| `-r, --retries N` | 每个文件最大重试次数（默认 10；`0`=无限，探测阶段仍始终有界） |
| `--max-time SECONDS` | 单个目标的墙钟上限（`0` = 不限） |
| `--probe-jobs N` | 解析/探测阶段的并发目标数（默认 8；`0` = 跟随 `--jobs`） |
| `--force` | 忽略已有文件/续传状态，从头重下 |
| `--skip-existing` | 只在**能证明已完成**时跳过（标记 / 完整位图 / 可证明无空洞；给出的校验和会先复验）；无见证的跳过会提示 `unversioned` |
| `--require-witness` | 拒绝跳过或续传**版本无法核对**的状态 |
| `--recheck` | 跳过前先复核已有文件：有摘要就重算，没有则要求版本见证（隐含 `--require-witness`） |
| `--checksum ALGO:HEX` | 下载后校验（如 `sha256:...`） |
| `--checksums FILE` | 按 `sha256sum`/`sha1sum`/`sha512sum`/`md5sum` 风格文件校验（每个目标都必须有对应条目） |
| `-p, --proxy URL` | 代理 |
| `-H, --header K:V` | 附加请求头（可多次） |
| `-u, --user USER:PASS` | HTTP Basic 认证 |
| `--mirror FROM=TO` | URL 前缀改写 |
| `--source NAME` | 强制指定源插件 |
| `--reporter NAME` | 进度呈现（`tqdm`/`json`/`quiet`） |
| `--include PAT` / `--exclude PAT` | 过滤通配（可多次） |
| `--revision REV` | 仓库 revision |
| `--dry-run` | 只探测不下载（也不写入或修改下载状态） |
| `--retry-failed` | 重试失败清单（按记录的原始目标重新解析；S3 会重新签名、私有仓库会重新取 token） |
| `--continue` | 目标解析/探测失败时跳过而非中断整批 |
| `-l, --list FILE` | 从文件读取目标清单（`-` = 标准输入） |
| `--hook NAME` | 本次运行启用某钩子（可多次） |
| `--user-agent UA` | 自定义 User-Agent |
| `--local-plugins` | 同时加载本地插件目录（不受信任） |

### 全局选项

以下适用于所有子命令（`-V/--version` 仅顶层，等价 `downctl version`）：

| 选项 | 说明 |
|---|---|
| `-q, --quiet` | 抑制非错误输出（并切换到 `quiet` 进度） |
| `-v, --verbose` | 额外诊断信息 |

下载会自动续传：中断后重跑同一命令即可。多连接下载每 `--checkpoint` 秒（默认 30）
把位图落盘一次，因此 `kill -9`、断电最多回退一个间隔；`Ctrl+C`、`SIGTERM`
（POSIX；Windows 控制台进程没有可捕获的 SIGTERM）会立即落盘。
完成状态记录在 `<文件>.downctl-done`（大小 + mtime + URL 身份；如校验过还会记录摘要）：它让下次运行无需相信
“文件大小刚好对”，让**已知大小**的完成文件在 WSL DrvFS / Windows 上也能直接跳过；
文件被外部修改后标记自动失效。未知大小或服务器不支持 Range 的单流下载没有位图，
普通重跑会整体重下；加 `--skip-existing` 才会凭该标记跳过。

0.1.4 起位图（`.downctl-pieces`）与完成标记（`.downctl-done`）还会记录远程对象的**版本见证**——强/弱 `ETag`，
退化时用 `Last-Modified`——因此**URL 不变但内容被替换**也会被识别：见证不同的位图会被
丢弃并从零重下，见证不同的标记会被拒绝（`--skip-existing` 不能把旧内容当成“已证明”）。
已知强见证时分片请求带 `If-Range`，且每个 `206` 的 `Content-Range` 必须精确覆盖请求区间，
否则在写入任何字节之前就被拒绝；条件请求收到完整正文时，会用一次额外探测区分“服务器
不支持 Range”（走单流）与“对象已被替换”（从零重启一次，`ContentChangedError`）。
服务器每次响应都换见证会被识别，该目标放弃见证继续下载（记为 unversioned）。
`--require-witness` 拒绝跳过或续传任何版本无法核对的状态；`--recheck` 在跳过前按记录的
或来源的摘要重新计算，并隐含 `--require-witness`。四种情形分别由
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`、
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`、
`tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`
与
`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`
覆盖。

见证是**纯增量字段**，不是新格式：`BITMAP_VERSION` 仍是 `4`、`COMPLETION_VERSION` 仍是 `2`，
旧版本读到时直接忽略——**降级回 0.1.3 是安全的**，不会截断任何文件。

`--skip-existing` 下，**用户提供**的校验和（`--checksum`/`--checksums`）会先复验再跳过；
来源自带的校验和（HF LFS、GitHub digest）只在完成标记记录了相同摘要时免于重算；
来源摘要变化、没有标记的完整位图或空洞扫描证明都会先重算再认可。

重试次数默认为有限值（`[core] retries = 10`）。`-r 0` 让传输循环恢复“无限重试”，但
解析/探测阶段始终有界：最多 3 次尝试、退避上限 5 秒、`Retry-After` 在探测与传输两条路径上
最多等待 300 秒
（`tests/integration/test_resilience.py::test_retry_after_is_honoured`）；
`408`/`425`/`429` 以外的 4xx 是服务器的最终答复，不会重试。`--max-time` 限制单个目标的
墙钟时间。

目标会以有界并发展开与探测（`--probe-jobs`，默认 8），而不是一次一个来回；失败报告仍按
命令行顺序
（`tests/integration/test_resilience.py::test_probe_phase_is_concurrent`、
`tests/integration/test_resilience.py::test_failure_order_follows_the_command_line`）。
每个 HTTP(S) 目标都会探测一次——**包括源已经报出大小的目标**——因为版本见证正是来自这次
请求；缺了它，传输只会发无条件的区间请求，写出的标记也永远发现不了后来的替换。

整批开始前，引擎会按**文件系统**汇总每个目标还需要的字节数（另加 200 MiB 余量）：
总数放不下时一次性失败并报出“需要多少、可用多少”，一个字节都不会写；并发数刻意不因此
降低——已经写下的字节仍然占着磁盘
（`tests/integration/test_resilience.py::test_a_batch_that_cannot_fit_never_starts_writing`）。

`--checksums` 采用失败关闭：每个被请求的目标都必须有条目；空清单或没有可用条目的清单
会在下载前直接报错。普通 `*sum` 文件无需 `algo:` 前缀，按摘要长度识别
md5/sha1/sha256/sha512。

失败项写入输出目录的 `.downctl.failed`；`--retry-failed` 会按记录的原始目标重新解析
（刷新 S3 预签名、带上当前 token）并原位重下。0.1.2 及更早版本的旧记录没有原始目标，
会回退到记录的 URL 并提示可能已过期。重试需要原始地址，因此该状态文件可能含有 URL
中的凭据；在 POSIX 上以仅属主可读写权限写入。

完成后会打印一行汇总：成功文件数、耗时、**本次实际传输字节**的平均速率与保存目录。
`--reporter json` 时 stdout 只输出 JSON 行（警告/错误仍在 stderr），人类汇总会被抑制。
`--dry-run` 与 JSON reporter 一起用时，每个目标输出一条 `dry_run` 记录
（`size`/`done`/`pending`/`witness`）。

## `downctl probe`

打印目标的大小与 HTTP 状态，不下载。

```
downctl probe <目标> [<目标> ...] [-p PROXY] [--json]
```

`--json` 时每个目标输出一行 JSON（`target`/`status`/`size`/`size_human`/
`reachable`/`content_type`/`final_url`/`filename`/`validator`/`witness`，
其中 `witness` 为 `etag`、`last-modified` 或 `none`）；文本模式亦会显示解析后的 URL、
`Content-Type`、推断文件名与服务器给出的版本见证。有目标不可达、HTTP 状态 ≥400 或不是
HTTP(S) 时退出码为 `1`（JSON 记录里带真实的 `status`；非 HTTP(S) 目标另带 `error`）。

## `downctl status`

扫描目录（默认配置的 `dir`），显示未完成（位图）与失败清单；未完成项会显示
进度、大小与最后修改时间。

```
downctl status [DIR] [--json]
```

`--json` 输出单个对象：`directory`、`incomplete`（`target`/`done`/`pieces`/
`percent`/`size`，以及可选的 `age_seconds`）与 `failed`。

## `downctl verify`

离线证明“声称已完成”的文件确实与来源一致。

```
downctl verify [目录] [--json] [--checksums FILE] [--repair]
```

`目录` 下每个带**可识别** `<文件>.downctl-done` 标记的文件都会被判定，并报告所用见证：

| 状态 | 含义 |
|---|---|
| `ok` | 内容得到证明：`--checksums` 里（或标记里记录的）摘要匹配 |
| `mismatch` | 摘要不匹配，或文件在标记写入之后被改动 |
| `unverifiable` | 没有发现问题，但没有摘要可证明内容（只能核对标记的大小/mtime 绑定） |
| `missing` | 标记还在，数据文件已不存在 |

退出码：`0` = 检查过的文件全部得到证明；`1` = 至少一个 `mismatch` 或 `missing`；
`2` = 没有发现问题，但至少有一个 `unverifiable`。不加 `--checksums` 时，标记里没有摘要的
文件只能是 `unverifiable`，命令会给出提示。仍在下载中的位图会列在 JSON 的 `incomplete`
数组里，**不影响**退出码
（`tests/unit/test_cli_verify.py::test_verify_reports_incomplete_downloads_without_failing`）。

`--json` 输出单个对象：`directory`、`checked`、`ok`、`mismatch`、`unverifiable`、
`missing`、`repaired`、`entries`（每项含 `target`/`status`/`witness`，适用时还有
`algo`/`expected`/`actual`/`detail`）与 `incomplete`。

`--checksums FILE` 与 `get` 使用同一种 `sha256sum` 风格清单：无前缀摘要按长度识别
md5/sha1/sha256/sha512，且清单条目优先于标记里记录的摘要。空清单或没有可用条目的清单会在
检查任何文件之前报错。`--repair` 只删除**已被证明**坏掉的东西——`mismatch` 文件连同其
标记，或孤儿标记——属于破坏性操作；`unverifiable` 的文件绝不会被删除。

退出码与修复行为由
`tests/unit/test_cli_verify.py::test_verify_without_a_digest_is_unverifiable_not_ok`、
`tests/unit/test_cli_verify.py::test_verify_repair_deletes_only_what_was_proven_bad`
与
`tests/unit/test_cli_verify.py::test_verify_repair_keeps_an_unverifiable_file`
覆盖。

## `downctl clean`

清理残留产物（分片位图、失败清单、过期锁目录），不触碰已下载的数据文件；
完成标记（`.downctl-done`）属于状态而非垃圾：数据文件仍在时保留，数据文件已不存在
且内容确实是完成标记的孤儿文件会被删除；名字恰好以该后缀结尾但不是合法标记的文件
会保留（可能是用户数据）。

```
downctl clean [DIR] [--dry-run] [--json]
```

`--json` 只改变输出格式；不传 `--dry-run` 时仍会真正删除。对象包含
`items`/`count` 以及 `removed`/`failed`；删除失败会如实报告并以退出码 `1` 结束，
不会把失败算作成功。只删除可识别的位图、合法的孤儿完成标记、临时文件与已死进程的
锁目录；名字恰好以这些后缀结尾的用户文件或目录绝不会被删除。

## `downctl plugin`

```
downctl plugin list          # 按类别列出已发现插件
downctl plugin info <名称>   # 显示插件类别与优先级
```

本地插件目录（`./downctl_plugins`、`~/.config/downctl/plugins`）只有显式传
`--local-plugins` 才会加载；默认列插件不会执行任何本地代码。

## `downctl config`

```
downctl config path          # 打印配置文件路径
downctl config init [--force]# 写入默认配置
downctl config show          # 打印生效配置（token/密码等已掩码）
```

## `downctl doctor`

打印版本、Python、平台、依赖版本、配置路径、插件数量与探测到的代理。

## `downctl wizard`

交互式输入目标与目录后开始下载。

## `downctl completions`

输出 shell 补全脚本，无需额外依赖：

```
downctl completions bash|zsh|fish
# bash: downctl completions bash >> ~/.bashrc
# zsh:  downctl completions zsh  > "${fpath[1]}/_downctl"
```

## 配置文件

`~/.config/downctl/config.toml`（`DOWNCTL_CONFIG` 可覆盖），值支持 `${VAR}` 插值。

完整键列表见[首页的配置一节](index.md)。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 有一个或多个目标失败 |
| 130 | 被中断（进度已保留，重跑即续传） |

`downctl probe` 在任一目标不可达、非 HTTP(S) 或 HTTP 状态 ≥400 时返回 `1`。
`downctl verify` 有自己的退出码：`0` = 检查过的文件全部得到证明，`1` = 有内容不符或有
完成标记却没有数据文件，`2` = 没有发现问题但无法证明。
