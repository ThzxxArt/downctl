# 下载源

*源（source）*把用户输入解析成下载任务。`downctl` 会依据目标的 URI scheme
自动选择源，也可用 `--source NAME` 强制指定。

| Scheme | 源 | 目标写法 |
|---|---|---|
| `https://` `http://` | `url` | `https://host/path/file` |
| `hf://`（或裸 `org/name`） | `huggingface` | `hf://org/name[@revision]` |
| `gh://` | `github` | `gh://owner/repo[@tag]` |
| `s3://` | `s3` | `s3://bucket/key` 或 `s3://bucket/prefix/` |
| `sftp://` | `sftp` | `sftp://[user@]host[:port]/path` |
| `file://` | `file` | `file:///绝对路径` |

`--include PAT`（可多次）与 `--exclude PAT` 适用于会列举文件的源
（huggingface、github、s3 前缀、sftp 目录、file 目录）。

---

## `url` —— 普通链接

```bash
downctl get https://example.com/big.iso
downctl get https://example.com/a.bin https://example.com/b.bin -j 2
```

- 文件名取路径最后一段（URL 解码后仅保留文件名）。
- 服务器支持 `Range` 时启用多连接与续传；不支持则自动降级为单流。

---

## `huggingface` —— 仓库

```bash
downctl get hf://meta-llama/Llama-3-8B
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' --exclude '*consolidated*'
downctl get hf://datasets/squad@main
downctl get org/name                 # 裸 repo id 亦可
downctl get hf://org/repo@v1.0 -d models
```

- 通过 Hub API 展开仓库清单（支持 models 与 datasets）。
- 使用每个文件的 **LFS sha256** 做下载后校验。
- `--revision REV` 或 `@REV` 指定 revision（默认 `main`）。
- 配置（`[plugins.huggingface]`）：
  - `token` —— 私有仓库/限额令牌（或环境变量 `HF_TOKEN` /
    `HUGGING_FACE_HUB_TOKEN`），**仅发给 HF 主机**；
  - `mirror` —— 替换 `https://huggingface.co` 的基址，如
    `https://hf-mirror.com`。

---

## `github` —— Release 资源

```bash
downctl get gh://cli/cli
downctl get gh://cli/cli --include '*.tar.gz'
downctl get gh://owner/repo@v1.2.3
```

- 列出最新 release（或 `@tag`）的 assets。
- 若 API 提供 `sha256` 摘要则用于校验。
- 配置（`[plugins.github]`）：
  - `token` —— API 令牌（或环境变量 `GITHUB_TOKEN` / `GH_TOKEN`）。带 token 时
    资源通过 API asset URL 获取（`Accept: application/octet-stream`），凭据只停留在
    API 主机上，不会泄露给下载 CDN；
  - `api` —— API 基址（GitHub Enterprise）。

---

## `s3` —— S3 及兼容存储

```bash
downctl get s3://my-bucket/path/to/file.bin
downctl get s3://my-bucket/datasets/ --include '*.parquet'
```

- 对象通过 **SigV4 预签名 URL**（纯标准库）获取，因此普通 HTTP 传输即可
  支持 Range、多连接与续传。
- 前缀会以 `ListObjectsV2` 分页列举。
- 凭据来自 `[plugins.s3]` 或标准 AWS 环境变量；预签名 query 会在日志与
  失败清单中被**脱敏**。
- 键：`region`、`access_key`、`secret_key`、`session_token`、`endpoint`、
  `path_style`（默认 `true`）、`expires`（1–604800 秒）。

MinIO 示例：

```toml
[plugins.s3]
endpoint = "http://127.0.0.1:9000"
region = "us-east-1"
access_key = "minioadmin"
secret_key = "minioadmin"
path_style = true
```

---

## `sftp` —— 走 SSH（可选依赖）

```bash
pip install 'downctl[sftp]'
downctl get sftp://user@host:22/home/user/data.bin
downctl get sftp://user@host/data/ --include '*.csv'
```

- 以 `/` 结尾（或为目录）时列举其中的文件，否则直接下载该文件。
- SFTP 传输支持按偏移 seek 的 Range，因此多连接与续传照常可用。
- 存在 `~/.ssh/known_hosts` 时**默认校验主机密钥**；
  `known_hosts = ""`（或 `none`/`false`）可关闭校验。
- 凭据可写在 URL（`user:pass@`）或 `[plugins.sftp]`
  （`user`、`password`、`key`、`key_passphrase`、`port`、`known_hosts`）。
  URL 中的密码会从任务与日志中剥离。

---

## `file` —— 本地文件

```bash
downctl get file:///data/models/        # 递归复制目录
downctl get file:///data/a.bin -d /backup
```

- 目录会递归遍历，`--include`/`--exclude` 过滤路径。
- 通过本地 `file` 传输读取（支持 Range），因此同样走统一的位图/续传流程，
  适合复制到另一个目录。
