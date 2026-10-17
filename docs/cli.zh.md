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
| `-r, --retries N` | 每个文件最大重试次数（`0`=无限） |
| `--force` | 忽略已有文件/续传状态，从头重下 |
| `--skip-existing` | 只在**能证明已完成**时跳过（标记 / 完整位图 / 可证明无空洞） |
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
换了 URL（revision、镜像对象、同名不同源）会丢弃旧位图，绝不拼接两个对象的字节。
`--skip-existing` 下，**用户提供**的校验和（`--checksum`/`--checksums`）会先复验再跳过；
来源自带的校验和（HF LFS、GitHub digest）只在完成标记记录了相同摘要时免于重算；
来源摘要变化、没有标记的完整位图或空洞扫描证明都会先重算再认可。

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
（`size`/`done`/`pending`）。

## `downctl probe`

打印目标的大小与 HTTP 状态，不下载。

```
downctl probe <目标> [<目标> ...] [-p PROXY] [--json]
```

`--json` 时每个目标输出一行 JSON（`target`/`status`/`size`/`size_human`/
`reachable`/`content_type`/`final_url`/`filename`）；文本模式亦会显示解析后的 URL、
`Content-Type` 与推断文件名。有目标不可达、HTTP 状态 ≥400 或不是 HTTP(S) 时
退出码为 `1`（JSON 记录里带真实的 `status`；非 HTTP(S) 目标另带 `error`）。

## `downctl status`

扫描目录（默认配置的 `dir`），显示未完成（位图）与失败清单；未完成项会显示
进度、大小与最后修改时间。

```
downctl status [DIR] [--json]
```

`--json` 输出单个对象：`directory`、`incomplete`（`target`/`done`/`pieces`/
`percent`/`size`，以及可选的 `age_seconds`）与 `failed`。

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
