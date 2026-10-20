# Sources

A *source* turns a target you type into download tasks. `downctl` picks the
source automatically from the target's URI scheme; `--source NAME` forces one.

| Scheme | Source | Target form |
|---|---|---|
| `https://` `http://` | `url` | `https://host/path/file` |
| `hf://` (or bare `org/name`) | `huggingface` | `hf://org/name[@revision]` |
| `gh://` | `github` | `gh://owner/repo[@tag]` |
| `s3://` | `s3` | `s3://bucket/key` or `s3://bucket/prefix/` |
| `sftp://` | `sftp` | `sftp://[user@]host[:port]/path` |
| `file://` | `file` | `file:///abs/path` |

Common filters `--include PAT` (repeatable) and `--exclude PAT` apply to the
listing sources (huggingface, github, s3 prefixes, sftp directories, file
directories).

---

## `url` — plain links

```bash
downctl get https://example.com/big.iso
downctl get https://example.com/a.bin https://example.com/b.bin -j 2
```

- Filename is the final path segment (URL-decoded, basename only).
- Server `Range` support enables multi-connection + resume; if a server
  ignores `Range` it falls back to a single stream automatically.

---

## `huggingface` — repositories

```bash
downctl get hf://meta-llama/Llama-3-8B
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' --exclude '*consolidated*'
downctl get hf://datasets/squad@main
downctl get org/name                 # bare repo id also works
downctl get hf://org/repo@v1.0 -d models
```

- Lists the repository via the Hub API (models and datasets).
- Uses each file's **LFS sha256** for post-download verification.
- `--revision REV` or `@REV` pins a revision (default `main`).
- Options (`[plugins.huggingface]`):
  - `token` — access token for private repos / higher limits (or env
    `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`). Sent to the host that issued it;
    with a `mirror` configured it is **withheld** unless `token_to_mirror` is on.
  - `mirror` — base host instead of `https://huggingface.co`, e.g.
    `https://hf-mirror.com`.
  - `token_to_mirror` — send the token to `mirror` too (default `false`).

---

## `github` — release assets

```bash
downctl get gh://cli/cli
downctl get gh://cli/cli --include '*.tar.gz'
downctl get gh://owner/repo@v1.2.3
```

- Lists assets of the latest release (or a `@tag`).
- Uses the asset `sha256` digest when the API provides one.
- Options (`[plugins.github]`):
  - `token` — API token (or env `GITHUB_TOKEN` / `GH_TOKEN`). With a token,
    assets are fetched through the API asset URL
    (`Accept: application/octet-stream`) so the credential stays on the API
    host and never leaks to the download CDN.
  - `api` — API base URL (GitHub Enterprise).

---

## `s3` — S3-compatible storage

```bash
downctl get s3://my-bucket/path/to/file.bin
downctl get s3://my-bucket/datasets/ --include '*.parquet'
```

- Objects are fetched through **SigV4 presigned URLs** (stdlib only), so the
  normal HTTP transport handles ranges, multiple connections and resume.
- The signature covers the HTTP method, so a presigned URL answers `HEAD` with
  `403` even though the ranged `GET` works. The probe treats that as
  "method-scoped rejection", not as a verdict on the object, and falls back to a
  ranged `GET` — so an S3 target still gets its size and version witness.
- A prefix is listed with `ListObjectsV2` (paginated).
- Credentials come from `[plugins.s3]` or the standard AWS env vars; the
  presigned query is redacted from logs, JSON and hook payloads. The failed log
  stores the original `s3://bucket/key` origin, so `--retry-failed` mints a
  fresh signature instead of replaying an expired URL.
- Options: `region`, `access_key`, `secret_key`, `session_token`, `endpoint`,
  `path_style` (default `true`), `expires` (1–604800 s). An `endpoint` mounted
  under a path (`https://gateway.example/s3`) keeps that prefix in both the
  signature and the URL.
- A download that outlives the signature is re-signed during the run; only a
  `403` triggers that, and it is bounded.

Example with MinIO:

```toml
[plugins.s3]
endpoint = "http://127.0.0.1:9000"
region = "us-east-1"
access_key = "minioadmin"
secret_key = "minioadmin"
path_style = true
```

---

## `sftp` — over SSH (optional dependency)

```bash
pip install 'downctl[sftp]'
downctl get sftp://user@host:22/home/user/data.bin
downctl get sftp://user@host/data/ --include '*.csv'
```

- A trailing `/` (or a directory) is listed; a file is downloaded directly.
- The SFTP transport honours byte ranges (seek), so ranges/multi-connection/
  resume all work.
- Host keys are **verified** against `~/.ssh/known_hosts` when present;
  `known_hosts = ""` (or `none`/`false`) disables verification.
- Credentials may be in the URL (`user:pass@`) or in `[plugins.sftp]`
  (`user`, `password`, `key`, `key_passphrase`, `port`, `known_hosts`).
  Passwords in URLs are stripped from tasks and logs.

---

## `file` — local files

```bash
downctl get file:///data/models/        # copy a directory recursively
downctl get file:///data/a.bin -d /backup
```

- Directories are walked recursively; `--include`/`--exclude` filter paths.
- Uses a local `file` transport with range support, so the same pipeline
  (bitmap, resume) applies — handy for copying into another directory.
- The resume identity of a local source includes its size, mtime, ctime and
  inode, so editing the source file (even to the same size, or with a restored
  mtime) starts a clean transfer instead of mixing old and new bytes. On POSIX
  `ctime` is the metadata-change time; on Windows it is the creation time, so
  that guard is weaker there — use `--checksum` when it matters. A tool that
  also restores ctime and inode (for example a full filesystem snapshot) can
  still defeat this heuristic; `--checksum` is the cryptographic proof.
