# 实战配方

实用示例，均假设 `downctl` 已在 `PATH` 中。

## 镜像下载模型

```bash
downctl get hf://meta-llama/Llama-3-8B \
  --include '*.safetensors' --exclude '*.gguf' \
  -d ~/models/llama3 -j 4
```

只拉权重，4 个文件并发。配置 `[plugins.huggingface] mirror =
"https://hf-mirror.com"` 走镜像；私有/受限仓库用 `token`（或 `HF_TOKEN`）。

## 端到端校验

```bash
# 单文件
downctl get https://example.com/model.bin --checksum sha256:<hex>

# 整个 release，对照项目的 SHA256SUMS
downctl get gh://owner/repo --checksums SHA256SUMS
```

不匹配会自动重下（最多两次）再失败，并记入 `.downctl.failed`，可用
`--retry-failed` 重试。

## 超大文件跨会话续传

无需额外操作——重跑同一命令即可：

```bash
downctl get https://example.com/huge.iso -d /data
# 按 Ctrl+C、重启机器……
downctl get https://example.com/huge.iso -d /data   # 续传，只补缺失分片
```

可随意调整连接数；改变 `--connections` 会**按字节重切位图**，而不是从头开始：

```bash
downctl get https://example.com/huge.iso --connections 4   # 保留进度
```

## 每晚镜像一个 S3 前缀

```bash
#!/usr/bin/env bash
set -euo pipefail
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
downctl get s3://bucket/datasets/ \
  --include '*.parquet' \
  -d /srv/datasets \
  --skip-existing \
  --retries 5
```

变化不大时 `--skip-existing` 让整次运行很轻。

## 拉取 GitHub 私有仓库的 release 资源

```bash
export GITHUB_TOKEN=ghp_xxx
downctl get gh://my-org/private-app --include '*.tar.gz' -d /opt/app
```

带 token 时资源通过 GitHub API 获取，凭据只留在 API 主机上。

## 事后用边车校验

```bash
# 下载并写 <文件>.sha256 边车
cat > ~/.config/downctl/config.toml <<'EOF'
[core]
hooks = ["checksum"]
EOF

downctl get https://example.com/a.bin https://example.com/b.bin -d data

# 之后用边车校验一份新拷贝
downctl get https://example.com/a.bin -d data2 --checksums data/a.bin.sha256
```

## 下载后自动解压

```bash
cat > ~/.config/downctl/config.toml <<'EOF'
[core]
hooks = ["archive"]

[plugins.archive]
keep = false
EOF

downctl get https://example.com/release.tar.gz
```

解压对路径穿越做了防护，并拒绝软/硬链接与设备文件。

## 批次完成后通知

```bash
[core]
hooks = ["exec"]

[plugins.exec]
command = "notify-send 'downctl' \"$DOWNCTL_TARGET 完成\""
events = ["on_task_finish"]
```

完整事件/环境变量见[钩子](hooks.md)。

## 从标准输入批量下载

```bash
printf '%s\n' \
  https://example.com/a.bin \
  https://example.com/b.bin | downctl get -l - -d ~/Downloads
```

## CI：缓存模型，容忍偶发故障

```bash
downctl get hf://org/model -d "$CACHE" \
  --skip-existing --retries 8 --stall 60 -j 2
```

## 诊断缓慢/卡住的下载

```bash
downctl -v get <url>     # 已解析任务、镜像改写、续传字节
downctl doctor           # 代理 / WSL / 依赖 / 插件
downctl status <目录>    # 哪些未完成或失败
```

若某主机频繁卡死，调小 `--stall`；若建立连接慢，调大它并调小 `--connections`。
