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
| `--piece-size SIZE` | 分片大小（`8M`/`4M`，默认自动） |
| `--min-split-size SIZE` | 小于该大小的文件走单连接 |
| `-t, --limit-rate RATE` | 限速（`5M`、`1m`、`512k`），**全局预算** |
| `-r, --retries N` | 每个文件最大重试次数（`0`=无限） |
| `--force` | 忽略已有文件/续传状态，从头重下 |
| `--skip-existing` | 已存在（且大小匹配）的文件跳过 |
| `--checksum ALGO:HEX` | 下载后校验（如 `sha256:...`） |
| `--checksums FILE` | 按 `sha256sum` 风格文件校验 |
| `-p, --proxy URL` | 代理 |
| `-H, --header K:V` | 附加请求头（可多次） |
| `-u, --user USER:PASS` | HTTP Basic 认证 |
| `--mirror FROM=TO` | URL 前缀改写 |
| `--source NAME` | 强制指定源插件 |
| `--reporter NAME` | 进度呈现（`tqdm`/`json`/`quiet`） |
| `--include PAT` / `--exclude PAT` | 过滤通配（可多次） |
| `--revision REV` | 仓库 revision |
| `--dry-run` | 只探测不下载 |
| `--retry-failed` | 重试失败清单 |
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

下载会自动续传：中断后重跑同一命令即可。失败项写入输出目录的
`.downctl.failed`；`--retry-failed` 会原位重下（含 repo 子路径与预期校验和）。

## `downctl probe`

打印目标的大小与 HTTP 状态，不下载。

```
downctl probe <目标> [<目标> ...] [-p PROXY] [--json]
```

`--json` 时每个目标输出一行 JSON（`target`/`status`/`size`/`reachable`）。

## `downctl status`

扫描目录（默认配置的 `dir`），显示未完成（位图）与失败清单。

```
downctl status [DIR]
```

## `downctl clean`

清理残留产物（分片位图、失败清单、过期锁目录），不触碰已下载的数据文件。

```
downctl clean [DIR] [--dry-run]
```

## `downctl plugin`

```
downctl plugin list          # 按类别列出已发现插件
downctl plugin info <名称>   # 显示插件类别与优先级
```

## `downctl config`

```
downctl config path          # 打印配置文件路径
downctl config init [--force]# 写入默认配置
downctl config show          # 打印生效配置
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
