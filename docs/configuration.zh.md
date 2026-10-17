# 配置

`downctl` 读取 `~/.config/downctl/config.toml`（可用环境变量 `DOWNCTL_CONFIG`
覆盖）。命令行参数优先于配置文件，值支持 `${环境变量}` 插值，未知键会被忽略。

```bash
downctl config path     # 配置文件位置
downctl config init     # 写入默认配置
downctl config show     # 打印生效配置
```

## 优先级

1. 命令行参数（最高）
2. 配置文件
3. 内置默认值（最低）

未在配置中设置时，会使用相应环境变量（如 `HF_TOKEN`、`GITHUB_TOKEN`、
AWS 凭据、`https_proxy`）。

## `[core]`

| 键 | 默认 | 含义 |
|---|---|---|
| `dir` | `~/Downloads` | 保存目录 |
| `jobs` | `1` | 并发文件数 |
| `retries` | `0` | 每文件重试次数（`0` = 无限重试） |
| `connections` | `16` | 每文件连接数 |
| `stall` | `30` | 卡死看门狗秒数 |
| `piece_size` | `0` | 显式分片字节数（`0` = 自动） |
| `min_split_size` | `0` | 小于此值的文件走单连接（`0` = 总是分片） |
| `rate` | 无 | 限速（字节/秒，**全局共享**）；`<= 0` = 不限速 |
| `proxy` | 无 | 代理地址 |
| `mirror` | `{}` | `FROM = "TO"` URL 前缀改写 |
| `hooks` | `[]` | 每次运行启用的钩子 |

## `[plugins.<名称>]`

每个插件读取各自的配置表。

| 插件 | 键 |
|---|---|
| `huggingface` | `token`、`mirror` |
| `github` | `token`、`api` |
| `s3` | `region`、`access_key`、`secret_key`、`session_token`、`endpoint`、`path_style`、`expires` |
| `sftp` | `user`、`password`、`key`、`key_passphrase`、`port`、`known_hosts` |
| `tqdm` | `enabled` |
| `logfile` / `archive` / `webhook` / `manifest` / `exec` / `checksum` | 见[钩子](hooks.md) |

`s3.expires` 默认 3600 秒（1–604800）；`s3.path_style` 默认 `true`。

## 完整示例

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 3
connections = 16
stall = 30
# piece_size = 8388608
# min_split_size = 1048576
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["archive", "manifest"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"

[plugins.s3]
# region = "us-east-1"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# session_token = "${AWS_SESSION_TOKEN}"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# path_style = true
# expires = 3600

[plugins.sftp]
# user = "me"
# password = "${SFTP_PASSWORD}"
# key = "~/.ssh/id_ed25519"
# port = 22
# known_hosts = "~/.ssh/known_hosts"

[plugins.archive]
# into = "~/Downloads/extracted"
# keep = true

[plugins.webhook]
# url = "https://example.com/downctl"
# events = ["on_finish"]

[plugins.manifest]
# filename = ".downctl.manifest"

[plugins.tqdm]
enabled = true
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `DOWNCTL_CONFIG` | 配置文件路径 |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | HuggingFace 令牌 |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API 令牌 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 凭据 |
| `https_proxy` / `HTTP_PROXY` 等 | 代理 |
| `WSL_HOST_IP` | WSL 代理自动探测用的宿主 IP |
| `NO_COLOR` | 关闭彩色输出 |
| `DOWNCTL_TRUST_PARTIAL` | 信任“大小已达预期但无法探测空洞”的本地文件 |
| `DOWNCTL_RETRY_BASE` | 重试退避基数秒数（默认 5） |

## 校验

加载时会校验数值键：`jobs`/`connections`/`stall` 需 `>= 1`，`retries >= 0`，
`rate <= 0` 视为不限速。非法值会以清晰信息终止。
