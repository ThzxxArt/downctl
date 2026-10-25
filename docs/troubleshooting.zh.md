# 故障排查

先用内置诊断：

```bash
downctl doctor          # 版本、Python、依赖、代理、插件数
downctl -v get <url>    # 详细模式（意外错误会打印堆栈）
```

## 进度显示

**为什么没有进度块？**
`auto` 只在 stderr 是终端且 `TERM` 不是 `dumb` 时画活块；管道仍保持 0.1.6 的
“无进度噪音”约定；`-q`、`--progress off`、`[plugins.live] enabled = false` 或
`[plugins.tqdm] enabled = false` 都会关掉它。`NO_COLOR` 只关颜色。想强制显示用
`--progress live`；遇到管道会退回逐行输出，而不是把光标转义写进日志。

**CI / 日志里需要进度。**
用 `--progress plain`：每 15 秒一行，失败/跳过文件各一行，不写 ANSI。
`--progress off` 是静默选项。

**进度块乱码，或 Ctrl-C 后光标不见了。**
reporter 在正常完成、异常和 `Ctrl-C` 时都会恢复光标（CLI 的 guard 在所有退出路径
关闭活块）。`kill -9` 无法执行清理，但下一次 shell 提示符通常不受影响。若某个终端
处理不了活块，改用 `--progress plain`。钩子的子进程若也往同一终端写（例如 `exec`），
同样会覆盖活块；这类钩子请配 `--progress plain`。

**条到 100% 了却像卡住？**
多半在哈希。文件行会变成 `verifying sha256`；引擎在读取文件前就发出这个阶段。
没有 `--checksum` 时不会哈希，文件会立即结束。

**为什么 ETA 显示 `--:--`？**
要么大小未知（单流服务器），要么最近三秒没有字节样本。呈现层不会把过期平均值
包装成确定的估计；`rate` 显示 `--` 是同一个原因。

**想回到 0.1.6 的进度条。**
用 `--reporter tqdm`；`[core] progress = "auto"` 仍以新的 `live` 活块为默认。

## 下载

**中断后会从头开始吗？**
不会。按分片位图续传，重跑同一命令只补缺失分片。多连接下载每 `--checkpoint` 秒
（默认 30）把位图落盘一次，`kill -9`/断电最多丢失一个间隔；`Ctrl+C`、`SIGTERM`
（POSIX；Windows 控制台进程没有可捕获的 SIGTERM）会立即收尾落盘。未知大小或服务器
不支持 Range 时走单流，没有位图，会从头下载（已完成的文件可用 `--skip-existing`
凭标记跳过）。（`DOWNCTL_TRUST_PARTIAL=1` 只影响“大小已达预期、但无法探测稀疏
空洞”的本地文件是否判为完整，**不是**用来续传比预期更小的部分文件。）

**重跑会全部重下吗？**
不会。已知大小的完成文件带 `<文件>.downctl-done`（大小 + mtime + URL 身份），
重跑时直接跳过。别的工具放进去的文件没有标记：Linux 原生文件系统会证明无空洞；
WSL DrvFS / Windows 上则宁可从源补齐，不赌内容完整。`--skip-existing` 遵循同一规则，
并对显式给出的校验和先复验。

**大小对得上的文件为什么又下了一遍？**
这是刻意的：大小相同不代表中间字节存在（多连接中断会留下空洞）。除非有完成标记、
完整位图，或在原生 POSIX 文件系统上通过空洞扫描证明无空洞，否则会重置旧文件。
WSL DrvFS / Windows 上没有标记的文件会重新下载；确认文件完整且来自其他工具时，
可在该次运行设 `DOWNCTL_TRUST_PARTIAL=1`。

**已经有这个文件，为什么又下了一遍？**
三种原因，都与“证据”有关：① 服务器报告的版本变了（强/弱 `ETag` 或 `Last-Modified`
改变），旧账本与旧完成标记被拒绝——这正是 0.1.4 的修复，不是回归；② 用了
`--require-witness`（或 `[core] require_witness = true`），任何版本无法核对的状态都不允许
跳过或续传，因此没有校验器的服务器会被重新下载；③ 用了 `--recheck`，即使没有
`--skip-existing` 也会先证明已有文件——有已知摘要就重算，不匹配就重下；无法证明的文件
同样会重下。三种情况都只影响该文件。

**服务器在范围响应里不带 ETag**
`206` 并不要求重复 validator。downctl 会接受字节，但把这次完成记为**未绑定**：不写入版本
见证，并警告该文件无法与某个 revision 绑定；`--require-witness` 下除非校验和能证明内容，
否则拒绝把它当作完成。若 `206` 带的是**不同**的 validator，仍会按拼接拒绝。

**换 revision / 换镜像后又要重下？**
位图绑定 URL 身份。`hf://repo@v1` 与 `hf://repo@v2`（或两个同大小但不同 URL 的对象）
是不同的内容，旧账本会被丢弃。S3 的预签名 query 每次轮换，但在计算身份时被忽略，
所以重新签名不会丢进度。0.1.4 起身份之外还看**版本见证**：URL 不变但内容被替换的对象
同样会重新下载，而不是续传到新 revision 里。

**提示磁盘空间不足**
每个文件下载前会做磁盘预检（保留 200 MiB 余量）；整批开始前还会按**文件系统**分组汇总
每个目标还需要的字节数。某一组总数放不下时，该文件系统上的目标会在写入前被拒绝并报出
“需要多少、可用多少”，其他挂载点上的目标照常开始
（`tests/integration/test_resilience.py::test_a_batch_that_cannot_fit_never_starts_writing`）。
并发数刻意不因此降低——已经写下的字节仍然占着磁盘。清出空间后重跑即可续传。

**`--max-time` 提前放弃了？**
`--max-time SECONDS` 限制单个目标的墙钟时间（`0` = 不限）。到点后该目标记为失败并写入
`.downctl.failed`，但已下载的分片保留，重跑即续传
（`tests/integration/test_resilience.py::test_max_time_gives_up_and_records_the_target`）。
只是卡住不动的服务器会更早被 `--stall` 看门狗捕获。

**传输像是卡住了**
连续 `--stall` 秒无进展会触发看门狗断开重连。可调整 `--stall`，或检查代理/网络。

**`checksum failed N times`**
文件反复校验不通过。请核对预期哈希（或来源）——服务端内容被改动时将永不通过。

**"unknown checksum algorithm"**
该算法没有对应校验器。内置支持：`sha256`、`sha1`、`sha512`、`md5`。
写错算法或未知算法会在**下载前**失败，而不是先下完再校验两次。

## 验证已完成文件

**`downctl verify` 退出码 3 是什么意思？**
退出码 `3` 表示没有发现**错误**，但也没有任何东西能被**证明**：完成标记里没有摘要，
内容也没有被重新计算。标记的大小/mtime 绑定只能发现一类外部改动：文件**长度**变了是
`mismatch`（退出码 `1`）；长度不变但 mtime 变了，无法证伪内容，会报 `unverifiable`
（退出码 `3`），`--repair` 绝不会删除它。两者都不是内容证明。加 `--checksums` 传一份
`sha256sum` 风格清单，就能把这些条目变成 `ok`
（`tests/unit/test_cli_verify.py::test_verify_without_a_digest_is_unverifiable_not_ok`）。
`--repair` 绝不会删除无法证明的文件
（`tests/unit/test_cli_verify.py::test_verify_repair_keeps_an_unverifiable_file`）。
退出码 `1` 才是真问题：有 `mismatch`（摘要不符或文件长度改变）或标记背后没有数据文件。
`2` 留给 argparse 用法错误。

## HTTP / 鉴权

**探测尝试三次后仍然失败？**
探测会重试瞬时故障——`5xx`、`429`、`408`、`425` 与网络中断——最多 3 次，退避与
`Retry-After` 都封顶 5 秒，然后抛 `ProbeError`。即使 `-r 0` 这个上限也仍然生效
（`tests/integration/test_resilience.py::test_probe_retries_stay_bounded`）。瞬时故障恢复后
是无感的：失败两次再成功仍会正常下载
（`tests/integration/test_resilience.py::test_a_transient_probe_failure_is_retried`）。
其它 4xx 是服务器的最终答复，不会重试——`404` 立即失败并写入 `.downctl.failed`
（`tests/integration/test_resilience.py::test_a_permanent_probe_failure_is_not_retried`）。
`401`/`403` 只有在**带区间的 `GET` 也同意**时才算最终答复：`HEAD` 被这样拒绝属于
**方法级**答复（预签名 URL 的签名绑定 HTTP 方法），探测会先改成带区间的 `GET` 再放弃。

**401/403/404**
私有资源或匿名限流。提供令牌（`HF_TOKEN`/`GITHUB_TOKEN` 或插件 `token`）。HF 令牌
只发给源解析出的那个主机——Hub，或配置了 `[plugins.huggingface] mirror` 且
`token_to_mirror = true` 时的镜像；因令牌被扣下而收到的 `401` 会点名这个键。
`--mirror` 是 URL 前缀改写，不是凭据：它换不来私有对象的访问权（镜像上没有该对象
照样 `404`），而且改写到别的主机时，令牌会随改写后的请求一起过去。若 `s3://` 目标
下载正常却报 `403`，那是预期行为：签名绑定方法，探测会自动回退到带区间的 `GET`。

**代理 / WSL**
用 `-p` 或 `https_proxy`/`HTTP_PROXY` 环境变量。WSL 下自动探测 Windows 宿主
代理；可用 `WSL_HOST_IP` 覆盖宿主地址。`downctl doctor` 会打印探测到的代理。

`-p` 与环境变量作用于每一个请求。自动探测是一个**猜测**：它假定 `/etc/resolv.conf`
里的宿主 IP 上、7897 端口有代理——所以它只作用于远程目标：主机是 `localhost`、回环/
链路本地/未指定地址，或命中 `no_proxy` 的目标一律直连。如果那里根本没有监听，所有
**远程**请求都会打到一个死端口上——要彻底关掉它，把 `WSL_HOST_IP` 设为 `127.0.0.1`
（只有这个确切值表示“没有宿主代理”），或者指定你实际使用的代理，或者用 `--no-proxy`
忽略所有代理（包括环境变量里的）。

**重定向到其他主机时令牌会跟着走吗？**
不会。令牌是**按主机**的（仅 HF/GitHub）；共享会话不带全局 `Authorization`，
因此重定向到无关主机会被丢掉
（`tests/integration/test_transfer.py::test_authorization_is_dropped_on_cross_origin_redirect`）。
显式路由是另一回事：`--mirror` 与 `-p` 作用于你自己要求发出的那个请求，令牌会跟着
改写到的主机走。

## SFTP

**"Host key is not trusted"**
默认按 `~/.ssh/known_hosts` 校验主机密钥。请把主机密钥加入，或用
`[plugins.sftp] known_hosts = ""`（也接受 `none`/`false`）关闭校验。

**"SFTP support requires asyncssh"**
安装可选依赖：`pip install 'downctl[sftp]'`。

## S3

预签名 URL 会过期（默认 1 小时）。超长传输可能超过有效期——调大
`[plugins.s3] expires`（最大 7 天）或重跑。

`--retry-failed` 不会重放过期地址：失败清单（v3 起记录原始目标，当前格式为 v6）
会保存原始 `s3://bucket/key`，重试时重新签名。**裸**预签名 `https://…` 目标没有可重新
解析的 `s3://` origin，它的 v6 记录会标记为不可重放，请重跑原 URL。0.1.2 及更早版本
生成的记录没有原始目标，会打印警告；请直接重跑原始的 `s3://` 目标。

## JSON / 脚本

`--reporter json` 在 stdout 每行输出一个对象（`run_start`、`start`、`progress`、
`resume`、`finish`、`failure`、`run_finish`）。`failure` 记录在解析/探测阶段失败的
目标，`run_finish.failed` 也会计入它们；`run_finish.hooks_failed` 单独统计钩子失败，
让脚本能区分“webhook 坏了”和“下载失败了”。有两个字段专门给脚本用，不必解析人类文案：

- `finish.witness` —— `validator`、`checksum` 或 `none`：`ok: true` 这个结论靠什么成立。
  版本无法核对而跳过的文件报 `none`
  （`tests/integration/test_resilience.py::test_a_json_skip_reports_the_witness_it_was_skipped_with`）。
- `failure.reason` 与 `finish.reason` —— `permanent`、`transient-exhausted` 或
  `timeout`：这个目标还值不值得再跑一次
  （`tests/integration/test_resilience.py::test_json_says_why_a_target_failed`）。

人类输出被抑制，警告/错误走 stderr，因此 `downctl get … --reporter json | jq` 可直接消费。

`downctl probe` 在任一目标不可达、非 HTTP(S) 或 HTTP 状态 ≥400 时退出码为 `1`；
`downctl status --json` 与 `downctl clean --json` 各输出单个 JSON 对象。

## 清理

```bash
downctl status <目录>    # 未完成 + 失败
downctl clean <目录>     # 清理残留位图 / 失败清单 / 失效锁
```

`clean` 绝不删除无法确认属主已死的锁目录，绝不触碰你的数据文件；数据文件仍在的
完成标记会保留，数据文件已不存在的孤儿标记会被清理。

## 失败原因

每次失败都会写进 `.downctl.failed`，失败分类那一列是四类原因之一（v6 还记录请求头的
名字、传输方式、写入主机与凭据标记）；JSON 流里的 `reason` 是同一个值。这一列回答的是
"重跑还有没有用"。

| 类别 | 含义 | 值得重跑吗 |
|---|---|---|
| `permanent` | 服务器给了明确答复（401/403/404）、没有源能处理该目标、或校验和规格写错。重跑不会改变任何字节 | 不值得——先修目标、凭据或校验和 |
| `transient-exhausted` | 探测一直失败（5xx、429/408/425、连接被断），直到有界的重试预算用完 | 值得——服务器可能已恢复 |
| `timeout` | 目标撞上了 `--max-time` | 值得，但需要更大的 `--max-time` 或更小的目标 |
| `unknown` | 旧格式记录，或早于分类机制的失败 | 在重跑证明之前先当作 `permanent` |

**唯一值得"什么都不改就重跑"的 403**：*预签名* URL 过期。签名会过期，而能重新签名的
源（s3）会在同一次运行里自己重签，不需要你重跑。

## 平台差异

同一条命令在不同平台上的**表现**略有差异（**结果**不会）。下面这些会影响你看到什么。

| 方面 | Linux / macOS | Windows | WSL |
|---|---|---|---|
| 路径 | POSIX 路径 | `D:\models` 处处可用 | `D:\models` 会被翻译成 `/mnt/d/models`，`downctl doctor` 可看结果 |
| 文件锁 | 带属主记录的锁目录 | 同上；被杀掉的进程按**退出码**判定，而不是"能不能打开它" | 同 Linux |
| 稀疏文件证明 | `SEEK_HOLE`/`SEEK_DATA` 可证明文件没有隐藏空洞 | 没有廉价空洞证明：无标记的文件不会被当作完整 | 同 Linux |
| `SIGTERM` | 转成 `KeyboardInterrupt` → 退出码 130，进度保留 | 控制台程序没有可捕获的 `SIGTERM`；关窗口即结束进程 | 同 Linux |
| 代理自动探测 | 无 | 无 | 会用 Windows 宿主 IP 猜一个代理，但**只对远程目标**生效；`localhost`、回环/链路本地/未指定地址与 `no_proxy` 命中一律直连。`WSL_HOST_IP=127.0.0.1` 可关闭猜测；`--no-proxy` 忽略所有代理 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 有一个或多个目标失败 |
| 130 | 被中断（进度已保留，重跑即续传） |
