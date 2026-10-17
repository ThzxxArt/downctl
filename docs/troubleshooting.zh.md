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

**换 revision / 换镜像后又要重下？**
位图绑定 URL 身份。`hf://repo@v1` 与 `hf://repo@v2`（或两个同大小但不同 URL 的对象）
是不同的内容，旧账本会被丢弃。S3 的预签名 query 每次轮换，但在计算身份时被忽略，
所以重新签名不会丢进度。

**提示磁盘空间不足**
每个文件下载前会做磁盘预检（保留 200MB 余量），只失败当前文件。清出空间后
重跑即可续传。

**传输像是卡住了**
连续 `--stall` 秒无进展会触发看门狗断开重连。可调整 `--stall`，或检查代理/网络。

**`checksum failed N times`**
文件反复校验不通过。请核对预期哈希（或来源）——服务端内容被改动时将永不通过。

**"unknown checksum algorithm"**
该算法没有对应校验器。内置支持：`sha256`、`sha1`、`sha512`、`md5`。
写错算法或未知算法会在**下载前**失败，而不是先下完再校验两次。

## HTTP / 鉴权

**401/403/404**
私有资源或匿名限流。提供令牌（`HF_TOKEN`/`GITHUB_TOKEN` 或插件 `token`），
或使用 `--mirror`。

**代理 / WSL**
用 `-p` 或 `https_proxy`/`HTTP_PROXY` 环境变量。WSL 下自动探测 Windows 宿主
代理；可用 `WSL_HOST_IP` 覆盖宿主地址。`downctl doctor` 会打印探测到的代理。

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
目标，`run_finish.failed` 也会计入它们。人类输出被抑制，警告/错误走 stderr，
因此 `downctl get … --reporter json | jq` 可直接消费。

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
