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

## 升级到 0.1.3 的变化

- **位图绑定 URL 身份（v4）**：0.1.2 及更早版本留下的未完成位图无法证明属于哪个 URL，
  首次续传时会被丢弃并从零开始。这是刻意的：旧账本可能把两个 revision 的分片拼在一起。
  已完成文件没有位图，不受这一条影响。
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
