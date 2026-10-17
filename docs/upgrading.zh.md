# 升级

`downctl` 采用标准打包方式，升级就是普通的安装/升级，无需先卸载。

## pip

```bash
python -m pip install --upgrade downctl          # 最新版
python -m pip install 'downctl==0.1.1'           # 指定版本
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

- 已下载的文件
- `~/.config/downctl/config.toml`（以及自定义的 `DOWNCTL_CONFIG`）
- 未完成的位图（`.downctl-pieces`）、`.downctl.failed`、锁目录

## 兼容性说明

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
