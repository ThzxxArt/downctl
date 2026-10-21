# 配置

`downctl` 读取 `$XDG_CONFIG_HOME/downctl/config.toml`（未设置 `XDG_CONFIG_HOME` 时为
`~/.config/downctl/config.toml`；可用环境变量 `DOWNCTL_CONFIG` 覆盖）。命令行参数优先于
配置文件，值支持 `${环境变量}` 插值。未知 `[core]` 键会作为警告报告，而不是被静默忽略：
写错的 `retires = 3` 过去会让默认值悄悄生效，毫无提示。

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
AWS 凭据、`https_proxy`）。`-p` 显式指定代理；`--no-proxy` 关闭所有代理（包括环境变量
与 WSL 猜测）；`--limit-rate 0` 表示不限速。

## `[core]`

| 键 | 默认 | 含义 |
|---|---|---|
| `dir` | `~/Downloads` | 保存目录 |
| `jobs` | `1` | 并发文件数 |
| `retries` | `10` | 每文件重试次数（`0` = 无限重试；探测阶段始终有界） |
| `connections` | `16` | 每文件连接数 |
| `stall` | `30` | 卡死看门狗秒数 |
| `checkpoint` | `30` | 续传状态落盘间隔（秒；`0` = 仅退出时） |
| `max_time` | `0` | 单个目标的墙钟上限（秒；`0` = 不限） |
| `probe_jobs` | `8` | 并发解析/探测的目标数（`0` = 跟随 `jobs`） |
| `require_witness` | `false` | 拒绝跳过或续传「无法核对对象版本」的状态 |
| `piece_size` | `0` | 显式分片字节数（`0` = 自动） |
| `min_split_size` | `0` | 小于此值的文件走单连接（`0` = 总是分片） |
| `rate` | 无 | 限速（字节/秒，**全局共享**）；`<= 0` = 不限速 |
| `proxy` | 无 | 代理地址 |
| `mirror` | `{}` | `FROM = "TO"` URL 前缀改写 |
| `hooks` | `[]` | 每次运行启用的钩子 |
| `strict_hooks` | `false` | 把钩子失败（解压抛错、命令非零退出）算作运行失败。钩子失败**始终**会被报告并计数，这里只决定退出码 |

### 并发语义

三个数字互相影响，含义各不相同：

| 选项 | 限制什么 | 作用于 |
|---|---|---|
| `jobs` | 同时打开多少个**文件** | 下载阶段 |
| `connections` | 单个文件可用多少**连接** | 一个文件分片之后 |
| `probe_jobs` | 同时解析/探测多少个目标 | 下载**之前**的阶段（`0` = 跟随 `jobs`） |

`connections` 是**单文件**上限，不是全局上限：`jobs=4` 且 `connections=16` 时最多可能有
64 条连接。相反，`rate` 是**全进程共享**的一份预算。小于 `min_split_size` 的文件无论
`connections` 多大都只走单连接。

## `[plugins.<名称>]`

每个插件读取各自的配置表。

| 插件 | 键 |
|---|---|
| `huggingface` | `token`、`mirror`、`token_to_mirror` |
| `github` | `token`、`api` |
| `s3` | `region`、`access_key`、`secret_key`、`session_token`、`endpoint`、`path_style`、`expires` |
| `sftp` | `user`、`password`、`key`、`key_passphrase`、`port`、`known_hosts` |
| `tqdm` | `enabled` |
| `logfile` / `archive` / `webhook` / `manifest` / `exec` / `checksum` / `metrics` / `notify` | 见[钩子](hooks.md) |

`s3.expires` 默认 3600 秒（1–604800）；`s3.path_style` 默认 `true`。

**凭据属于签发它的主机。** 配了 `mirror` 不等于同意把 token 交给那台主机，所以默认只有
`huggingface.co` 会收到 token；需要鉴权的镜像要显式写 `token_to_mirror = true`（若因为
token 被扣下而收到 401，错误信息会提示这个键）。

## `[auth."<主机>"]`

凭据按**主机**配置，而不是按源插件。每张表指定一个 provider 及其选项；
`downctl auth check URL` 会显示某台主机会命中哪条规则与哪个 provider。匹配与 provider
细节见[作业、凭据与交付](batch.md)。

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
ttl = 300          # 秒；0（默认）= 整次运行复用
```

- `provider` 必填；内置 `env`、`command`、`netrc`。
- 精确主机优先；否则最长 `*.suffix` 胜出，且通配符只匹配子域（不匹配 apex）。同一主机
  定义两次是错误。
- `header`/`prefix` 默认 `Authorization` 与 `Bearer `；`netrc` 返回 HTTP Basic。
- 材料绝不会写入配置、日志或 JSON；`auth check` 只打印头**名字**。
- 显式 `-H`/`-u` 优先于 provider；材料只发给请求的第一跳源。

## 完整示例

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 3
connections = 16
stall = 30
checkpoint = 30
# piece_size = 8388608
# min_split_size = 1048576
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["archive", "manifest"]

[auth."huggingface.co"]
# provider = "env"
# env = "HF_TOKEN"

[auth."*.corp.example"]
# provider = "netrc"

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
| `XDG_CONFIG_HOME` | 默认配置与本地插件目录的基准目录 |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | HuggingFace 令牌 |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API 令牌 |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 凭据 |
| `https_proxy` / `HTTP_PROXY` 等 | 代理 |
| `WSL_HOST_IP` | WSL 代理自动探测用的宿主 IP（设为 `127.0.0.1` 即关闭该猜测） |
| `NO_COLOR` | 关闭彩色输出 |
| `DOWNCTL_TRUST_PARTIAL` | 信任“大小已达预期但无法探测空洞”的本地文件 |
| `DOWNCTL_RETRY_BASE` | 重试退避基数秒数（默认 5） |

## 校验

加载时会校验数值键：`jobs`/`connections`/`stall` 需 `>= 1`，`retries >= 0`，
`checkpoint >= 0`（`0` 关闭周期落盘），`max_time >= 0`、`probe_jobs >= 0`
（`0` = 不限 / 跟随 `jobs`），`rate <= 0` 视为不限速。非法值会以清晰信息终止。
命令行覆盖同样校验（`--jobs 0`、`--stall 0`、`--retries -1`、`--max-time -1`、
`--probe-jobs -1`、`--piece-size 1` 都会被拒绝）。`--limit-rate 0` 合法，表示不限速。

`downctl config show` 打印**生效**配置（默认值 + 文件 + 环境 + 当前命令的覆盖），
掩码插件与 `[auth]` 里看起来像敏感值的键，以及代理中的用户信息；磁盘上的配置文件不变。
未知 `[core]` 键会给出警告。

`DOWNCTL_TRUST_PARTIAL=1` 只影响“满大小、无完成标记、且文件系统无法证明无空洞”的
本地文件判定；downctl 自己下载的文件带 `.downctl-done` 标记，不需要它。
