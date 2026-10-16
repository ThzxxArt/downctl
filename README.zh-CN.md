# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 一个基于插件架构的通用命令行文件下载器，支持断点续传、多连接加速与断线自愈。

**一切皆插件。**

`downctl` 可以可靠地从几乎任何地方下载文件。可续传的分片位图、多连接加速、
卡死检测与自动重试是稳定内核的一部分；其余能力——源、传输、校验、落地、
进度呈现、路由与生命周期钩子——全部是插件，且内置功能使用完全相同的插件接口。

> 当前状态：**pre-1.0**。下载内核（断点续传、多连接）与 CLI 已端到端可用。
> HuggingFace 仓库展开与更多源正在开发中。完整设计见
> [`docs/DESIGN.md`](docs/DESIGN.md)。

## 安装

```bash
pip install downctl
```

需要 Python 3.11+。

## 使用

```bash
# 下载一个文件
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# 下载 HuggingFace 仓库（内置源插件）
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# 只探测不下载
downctl probe https://example.com/big.iso

# 查看插件与环境
downctl plugin list
downctl doctor

# 查看未完成 / 失败的下载
downctl status ~/Downloads
```

## Docker

```bash
docker build -t downctl .
docker run --rm -v "$PWD:/data" downctl get <target>
```

## 插件

| 类型 | 职责 | 内置实现 |
|---|---|---|
| Source | 解析输入为下载任务 | `url`、`huggingface`、`github`、`s3` |
| Transport | 传输数据 | `http` |
| Verifier | 校验摘要 | `sha256` |
| Sink | 数据落地 | `filesystem` |
| Reporter | 进度与日志 | `tqdm`、`json`、`quiet` |
| Router | URL 改写 | `mirror` |
| Hook | 生命周期事件 | `logfile` |

插件从三处发现：内置、安装包声明的 `downctl.*` entry-points、
本地目录（`./downctl_plugins`、`~/.config/downctl/plugins`）。

### 编写插件

参考 [`examples/plugins/`](examples/plugins) 中的最小可安装插件包。插件是任意
对象，只需暴露元数据（`name`、`api_version`、`priority`）以及
[`downctl.plugin.protocols`](src/downctl/plugin/protocols.py) 中某个契约的方法。

> **安全提示：** 本地目录插件等价于执行任意代码。请只启用你信任的目录；
> entry-points 插件会自动加载。

## 开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check .
mypy
pytest
```

## 文档

- [命令行参考](docs/cli.md)
- [插件开发](docs/plugins.md)
- [设计文档](docs/DESIGN.md)
- [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [发布流程](RELEASING.md)

## 许可证

[MIT](LICENSE) © ThzxxArt
