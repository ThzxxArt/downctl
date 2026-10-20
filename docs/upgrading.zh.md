# 升级

`downctl` 采用标准打包方式，升级就是普通的安装/升级，无需先卸载。

## pip

```bash
python -m pip install --upgrade downctl          # 最新版
python -m pip install 'downctl==0.1.3'           # 指定版本
python -m pip install --upgrade 'downctl[sftp]'  # 含可选依赖
```

在受 PEP 668 保护的系统（Ubuntu 24.04+/Debian 12+）请使用虚拟环境、`pipx`
或 `--break-system-packages`。

## pipx

```bash
pipx upgrade downctl
pipx upgrade --include-injected downctl
pipx install --force 'downctl[sftp]'             # 加装可选依赖
```

## 源码 / 可编辑安装

```bash
git pull
python -m pip install --upgrade .                # 常规
python -m pip install --upgrade -e '.[dev,sftp]' # 可编辑/开发
```

## Docker

```bash
git pull
docker build -t downctl .
```

## 验证

```bash
downctl version                 # 打印版本
pip show downctl | grep Version # 或经 pip 查看
downctl doctor                  # 依赖 / 插件 / 代理
```

## 升级不会影响

- 安装时的已下载文件（Windows / WSL DrvFS 上首次运行可能重新下载没有标记的文件，见下）
- `~/.config/downctl/config.toml`（以及自定义的 `DOWNCTL_CONFIG`）
- `.downctl.failed`、锁目录、完成标记（`.downctl-done`）

## 升级到 0.1.5 的变化

"传递"：一次运行做出的每个决定——凭据发给哪台主机、代理能作用于哪个目标、完成声明
靠什么支撑——现在都会抵达真正执行它的那一行，而不是在路上被重新猜一次或直接丢掉。

**有三处默认行为变了**，每一处都是修复，而且升级后第一次运行就能看出来。

- **HuggingFace 令牌不再发给配置的 `mirror`。** 令牌属于签发它的主机，配置镜像从来不
  等于同意把凭据交给它。镜像需要鉴权时，显式设置
  `[plugins.huggingface] token_to_mirror = true`；因令牌被扣下而收到的 401 会点名这个
  键。从 `huggingface.co` 本身的下载不受影响。
- **"猜出来的" WSL 宿主代理不再作用于本地目标。** WSL 宿主代理自动探测仍会为远程请求
  填一个代理，但回环、链路本地以及 `no_proxy` 命中的目标改为直连。显式 `-p`、配置文件
  里的 `proxy`、或环境变量里的代理**保持原有作用范围**——包括本地目标，因为那是用户
  明确要求的。
- **证据钩子不再为无法证明的完成背书。** 当引擎报告 `witness = none`（一次无法与版本
  挂钩的跳过）时，`checksum` 钩子不写边车文件，`manifest` 也不列该条。这两种产物都会被
  `--checksums` 读回去，所以为未验证文件写一份边车，是一个没人兑现过的承诺。

其它值得知道的行为变化：

- **`file://` 源指向自身会被拒绝。** `downctl get file:///a/x -d /a`（或用 `-o` 指到
  源文件）过去在不带 `--force` 时无害，带上它就会在传输读取之前把目标截断——等于清空
  源文件。现在这是硬错误，`--dry-run` 也能提前报出来。
- **钩子配置会被校验，而不是直接赋值。** 未知键或类型不符会告警并跳过，而不是被静默
  丢弃；`events = "on_finish"`（字符串）会被归一化成列表——作为字符串时它会让钩子在
  `on_task_finish` 上触发，因为 `"on_finish" in "on_task_finish"` 为真；未知事件名会被
  拒绝。
- **钩子失败会被报告。** 过去后处理抛出的异常（加密压缩包、损坏的 tar、`timeout = "300"`）
  会跟着任务句柄一起消失、毫无输出。现在会给出告警；`[core] strict_hooks = true` 可让
  任何钩子失败反映到退出码 `1`。
- **`probe --json` 的字段由 `witness` 改名为 `validator_kind`。** 旧名字与"完成见证"
  的词表撞车：`probe` 描述的是*远端版本来自哪个响应头*（`etag` / `last-modified` /
  `none`），而别处的 `witness` 指*一次完成声明靠什么支撑*（`validator` / `checksum` /
  `none`）。读取 `probe --json` 的脚本需要改键名。
- **存储时带 `Content-Encoding` 的对象现在能正确下载。** 服务器无视 `Accept-Encoding`、
  对对象始终回 `Content-Encoding: gzip` 时，过去每个范围请求都因长度不符而失败；现在
  传输收到的就是 `Content-Length` 所描述的那些存储字节。
- **`--force` 与 `--skip-existing` 同时给出会直接拒绝**，而不是让其中一个悄悄胜出。
- **S3**：下载时间超过预签名有效期时会在同一次运行里重签，而不是以 `403` 失败；挂在
  子路径下的 `endpoint`（如 `https://gateway.example/s3`）会把前缀同时带进签名与 URL；
  重复给出同一个续页 token 的列表会被拒绝而不是死循环。
- **更新版本 downctl 写的状态文件绝不会被覆盖。** 如果你降级，来自新版本的账本或标记
  会被提示并原样保留。
- **`doctor` 会说明令牌来自哪里**（`config` 还是 `env`，以及哪个键）——这通常就是
  "为什么会 401" 的答案。

如果你用 0.1.0–0.1.3 下载过文件且从未验证过，请先读
[拼接文件安全公告](security-advisory-0.1.3-splicing.md)。

## 升级到 0.1.4 的变化

- **位图记录对象版本见证——格式版本号不变**：位图现在还会记录远程对象的版本见证（强/弱
  `ETag`，退化时用 `Last-Modified`）。格式版本被刻意保持不变（`BITMAP_VERSION` 仍是 `4`、
  `COMPLETION_VERSION` 仍是 `2`，见证是纯增量字段），因此**降级回 0.1.3 是安全的**：
  旧版本会忽略这个多出来的字段，而不是把文件当成无法证明的状态，不会截断任何内容。
  0.1.3 留下的位图没有见证，当服务器能报出版本时（正常情况）它会被丢弃，**未完成的**多连接
  下载从零重下一次；服务器完全不提供见证时没有可比对的依据，续传行为与 0.1.3 完全一致。
  已完成文件没有位图，不受影响，所以只有升级时仍在下载中的文件会重来一次。
- **完成标记保持兼容——格式版本号同样不变**：新标记会记录见证（`COMPLETION_VERSION` 仍是
  `2`）；0.1.3 时期的标记继续被信任，因此升级**不会**造成大规模重下
  （`tests/integration/test_content_truth.py::test_a_legacy_marker_keeps_being_trusted`）。
  下一次把这个目标真正跑到完成的运行会用当时观察到的见证**原位补写**该标记。若这次只是被
  `--skip-existing` 跳过，没有见证的标记会报告为 `unversioned` 并**刻意保持无见证**——把今天
  的见证盖到一份在未知版本下取得的文件上，会让下一次替换再也无法被发现
  （`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`）。
- **`sink` 插件类别已移除**：`downctl.sinks` 成为退役的 entry-point 组，仍声明它的安装包
  会在加载时收到警告（而不是被静默忽略），`downctl plugin list` 也不再列出 `sink`。
  `API_VERSION` 仍为 `"1"`，其余六类插件不受影响。
- **重试次数默认为有限值**（`[core] retries = 10`）：默认无限重试会把一个永久失败的服务器
  变成静默挂起。要恢复“无限重试”请设 `retries = 0`（或 `-r 0`）；解析/探测阶段无论怎样
  都保持有界（最多 3 次尝试）。
- **对象被替换现在会被识别**：0.1.3 可能在“URL 不变、内容已换”时继续续传，0.1.4 会丢弃账本
  并重下。这是修复而非回归：当服务器不再提供见证，或启用了 `--require-witness` / `--recheck`
  时，偶尔多下一次属于预期行为。
- **新增选项**：`downctl verify [DIR]`（离线证明；退出码 `1` = 有内容不符或数据文件缺失，
  `2` = 无法证明）、`--require-witness`、`--recheck`、`--max-time SECONDS`、
  `--probe-jobs N`。整批放不下时，磁盘预算会在写入任何字节之前直接失败。

## 升级到 0.1.3 的变化

- **位图绑定 URL 身份（v4）**：0.1.2 及更早版本留下的未完成位图无法证明属于哪个 URL，
  首次续传时会被丢弃并从零开始。已完成文件没有位图，不受这一条影响。这一版关闭的是
  URL/revision 维度（旧账本可能把两个 revision 的分片拼在一起）；**同 URL 但内容被替换**
  的对象版本维度由 0.1.4 关闭（见上）。
- **`--skip-existing` 更严格**：不再因为“大小相同”就跳过；必须有完成标记、完整位图，
  或可证明无空洞的文件；带 `--checksum` 时会先复验。
- **Windows / WSL DrvFS 更严格**：没有 `.downctl-done` 标记的满大小文件会重新下载，
  因为这些文件系统无法证明没有空洞。**0.1.2 及更早版本完成的文件也没有标记**，因此在这两类
  文件系统上首次重跑会重新下载一次；原生 Linux/macOS 通过空洞扫描证明后补写标记，不会重下。
  确认文件完整时可临时设 `DOWNCTL_TRUST_PARTIAL=1`。
- **`--reporter json` 的 stdout 保持纯净**：人类汇总被抑制，警告/错误走 stderr。
- **`downctl probe` 在目标不可达、非 HTTP(S) 或 HTTP ≥400 时退出码为 `1`**。
- **`downctl plugin` 默认不再执行本地插件目录**，除非显式传 `--local-plugins`。
- **失败清单 v3 记录原始目标**：`--retry-failed` 能刷新 S3 签名与私有仓库 token；
  旧记录仍可读取。

## 兼容性说明

- 0.1.3 要求 `aiohttp>=3.11`，pip 会自动升级。旧的最低版本允许安装到
  `--proxy` 在创建会话时就会崩溃的版本。
- 配置键向后兼容，无法识别的键会被忽略（`downctl config show` 打印生效值）。
- 0.1.1 起版本号来自包内 `__version__`，用 `downctl version` 确认。
- 插件声明插件 API 版本（`downctl.API_VERSION`，当前为 `1`）；未来若提升，
  不兼容的插件会被明确拒绝，而不是运行到一半才失败。

## 卸载

```bash
python -m pip uninstall downctl
# pipx uninstall downctl
```

配置与下载文件会保留（如需清理请手动删除）。
