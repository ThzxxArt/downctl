# 故障排查

先用内置诊断：

```bash
downctl doctor          # 版本、Python、依赖、代理、插件数
downctl -v get <url>    # 详细模式（意外错误会打印堆栈）
```

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
跳过或续传，因此没有校验器的服务器会被重新下载；③ 用了 `--recheck`，跳过前会按记录的或
来源的摘要重新计算。三种情况都只影响该文件。

**换 revision / 换镜像后又要重下？**
位图绑定 URL 身份。`hf://repo@v1` 与 `hf://repo@v2`（或两个同大小但不同 URL 的对象）
是不同的内容，旧账本会被丢弃。S3 的预签名 query 每次轮换，但在计算身份时被忽略，
所以重新签名不会丢进度。0.1.4 起身份之外还看**版本见证**：URL 不变但内容被替换的对象
同样会重新下载，而不是续传到新 revision 里。

**提示磁盘空间不足**
每个文件下载前会做磁盘预检（保留 200MB 余量）；整批开始前还会按**文件系统**汇总每个目标
还需要的字节数，总数放不下时一次性失败并报出“需要多少、可用多少”，一个字节都不会写
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

**`downctl verify` 退出码 2 是什么意思？**
退出码 `2` 表示没有发现**错误**，但也没有任何东西能被**证明**：完成标记里没有摘要，
内容也没有被重新计算。标记的大小/mtime 绑定仍能发现外部改动（那是 `mismatch`，退出码
`1`），但它不是内容证明。加 `--checksums` 传一份 `sha256sum` 风格清单，就能把这些条目
变成 `ok`（`tests/unit/test_cli_verify.py::test_verify_without_a_digest_is_unverifiable_not_ok`）。
`--repair` 绝不会删除无法证明的文件
（`tests/unit/test_cli_verify.py::test_verify_repair_keeps_an_unverifiable_file`）。
退出码 `1` 才是真问题：有 `mismatch`（摘要或大小/mtime 不符）或标记背后没有数据文件。

## HTTP / 鉴权

**探测尝试三次后仍然失败？**
探测会重试瞬时故障——`5xx`、`429`、`408`、`425` 与网络中断——最多 3 次，退避上限 5 秒，
`Retry-After` 最多等待 300 秒，然后抛 `ProbeError`。即使 `-r 0` 这个上限也仍然生效
（`tests/integration/test_resilience.py::test_probe_retries_stay_bounded`）。瞬时故障恢复后
是无感的：失败两次再成功仍会正常下载
（`tests/integration/test_resilience.py::test_a_transient_probe_failure_is_retried`）。
其它 4xx 是服务器的最终答复，不会重试——`404` 立即失败并写入 `.downctl.failed`
（`tests/integration/test_resilience.py::test_a_permanent_probe_failure_is_not_retried`）。
`401`/`403` 只有在**带区间的 `GET` 也同意**时才算最终答复：`HEAD` 被这样拒绝属于
**方法级**答复（预签名 URL 的签名绑定 HTTP 方法），探测会先改成带区间的 `GET` 再放弃。

**401/403/404**
私有资源或匿名限流。提供令牌（`HF_TOKEN`/`GITHUB_TOKEN` 或插件 `token`），
或使用 `--mirror`。若 `s3://` 目标下载正常却报 `403`，那是预期行为：签名绑定方法，
探测会自动回退到带区间的 `GET`。

**代理 / WSL**
用 `-p` 或 `https_proxy`/`HTTP_PROXY` 环境变量。WSL 下自动探测 Windows 宿主
代理；可用 `WSL_HOST_IP` 覆盖宿主地址。`downctl doctor` 会打印探测到的代理。

自动探测是一个**猜测**：它假定 `/etc/resolv.conf` 里的宿主 IP 上、7897 端口有代理。
如果那里根本没有监听，所有请求都会打到一个死端口上——要彻底关掉它，把
`WSL_HOST_IP` 设为 `127.0.0.1`（任何回环值都表示"没有宿主代理"），或者用 `-p`
指定你实际使用的代理。`-p ""` 无法表示"不用代理"，因为"没设置"和"故意设为空"
在配置里长得一模一样。

**重定向到其他主机时令牌会跟着走吗？**
不会。令牌是**按主机**的（仅 HF/GitHub）；共享会话不带全局 `Authorization`，
因此凭据不会发往无关主机。

## SFTP

**"Host key is not trusted"**
默认按 `~/.ssh/known_hosts` 校验主机密钥。请把主机密钥加入，或用
`[plugins.sftp] known_hosts = ""`（也接受 `none`/`false`）关闭校验。

**"SFTP support requires asyncssh"**
安装可选依赖：`pip install 'downctl[sftp]'`。

## S3

预签名 URL 会过期（默认 1 小时）。超长传输可能超过有效期——调大
`[plugins.s3] expires`（最大 7 天）或重跑。

`--retry-failed` 不会重放过期地址：失败清单 v3 记录原始 `s3://bucket/key`，
重试时重新签名。0.1.2 及更早版本生成的记录没有原始目标，会打印警告；
请直接重跑原始的 `s3://` 目标。

## JSON / 脚本

`--reporter json` 在 stdout 每行输出一个对象（`run_start`、`start`、`progress`、
`resume`、`finish`、`failure`、`run_finish`）。`failure` 记录在解析/探测阶段失败的
目标，`run_finish.failed` 也会计入它们。有两个字段专门给脚本用，不必解析人类文案：

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

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 有一个或多个目标失败 |
| 130 | 被中断（进度已保留，重跑即续传） |
