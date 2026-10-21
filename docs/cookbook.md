# Cookbook

Practical recipes. All examples assume `downctl` is on your `PATH`.

## Mirror model downloads

```bash
downctl get hf://meta-llama/Llama-3-8B \
  --include '*.safetensors' --exclude '*.gguf' \
  -d ~/models/llama3 -j 4
```

Only the weights are fetched, four files at a time. Set
`[plugins.huggingface] mirror = "https://hf-mirror.com"` to route through a
mirror, and `token` (or `HF_TOKEN`) for gated repositories.

## Verify a download end to end

```bash
# one file
downctl get https://example.com/model.bin --checksum sha256:<hex>

# a whole release, against the project's SHA256SUMS
downctl get gh://owner/repo --checksums SHA256SUMS
```

A mismatch re-downloads **once** — a second mismatch gives up — and the file is
recorded in `.downctl.failed` for `--retry-failed`.

Later — or on another machine that has the files but not the origin — prove what
is on disk without touching the network:

```bash
downctl verify ~/models --checksums SHA256SUMS   # exit 0 = proven, 3 = unprovable
downctl verify ~/models --repair                 # delete only what was proven bad
```

Every file that carries a completion marker is reported as `ok`, `mismatch`,
`unverifiable` or `missing`, together with the witness that decided it (see the
[CLI reference](cli.md)).

## Resume a huge download across sessions

Nothing special to do — re-run the same command:

```bash
downctl get https://example.com/huge.iso -d /data
# Ctrl+C, machine reboot, whatever...
downctl get https://example.com/huge.iso -d /data   # resumes, only missing pieces
```

Tune the connection count freely; changing `--connections` **reslices** the
bitmap rather than restarting:

```bash
downctl get https://example.com/huge.iso --connections 4   # keeps progress
```

## Nightly mirror of an S3 prefix

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

`--skip-existing` makes the run cheap when little changed: a file previously
completed by downctl carries a `.downctl-done` marker and is skipped without a
data request. Files placed by other tools are only skipped when their
completeness is provable (native Linux hole scan; elsewhere they are re-fetched
unless `DOWNCTL_TRUST_PARTIAL=1` is set). Failed files are recorded with their
`s3://` origin, so a later `--retry-failed` mints a fresh signature.

## Fetch a private GitHub release asset

```bash
export GITHUB_TOKEN=ghp_xxx
downctl get gh://my-org/private-app --include '*.tar.gz' -d /opt/app
```

With a token, assets are fetched through the GitHub API so credentials stay on
the API host.

## Verify after the fact with a sidecar

```bash
# download + write <file>.sha256 sidecars
cat > ~/.config/downctl/config.toml <<'EOF'
[core]
hooks = ["checksum"]
EOF

downctl get https://example.com/a.bin https://example.com/b.bin -d data

# later, re-verify a fresh copy against the sidecars
downctl get https://example.com/a.bin -d data2 --checksums data/a.bin.sha256
```

## Auto-extract archives

```bash
cat > ~/.config/downctl/config.toml <<'EOF'
[core]
hooks = ["archive"]

[plugins.archive]
keep = false
EOF

downctl get https://example.com/release.tar.gz
```

Extraction is hardened against path traversal and refuses links/devices.

## Notify when a batch finishes

```toml
[core]
hooks = ["exec"]

[plugins.exec]
command = "notify-send 'downctl' \"$DOWNCTL_TARGET done\""
events = ["on_task_finish"]
```

For a simpler notifier, use the built-in hook:

```toml
[core]
hooks = ["notify"]

[plugins.notify]
url = "https://ntfy.sh/my-downctl"
only_failures = true
```

See [`hooks.md`](hooks.md) for the full event/variable list.

## Run several jobs from one file

```toml
version = 1

[[job]]
name = "llama"
targets = ["hf://meta-llama/Llama-3-8B@main"]
dir = "models/llama"
include = ["*.safetensors", "*.json"]
checksums = "checksums.txt"

[[job]]
name = "tools"
targets = ["https://example.com/tool.tar.gz"]
checksum = "sha256:9f2c..."
hook = ["archive"]
```

```bash
downctl batch jobs.toml --dry-run          # validate + probe every job
downctl batch jobs.toml --job llama        # run one job
downctl batch jobs.toml --continue         # keep later jobs after a failure
```

Every job is validated before the first download. Exit `2` means the file is
invalid and nothing was downloaded; exit `1` means a target or job failed. See
[Jobs, credentials and serving](batch.md).

## Keep a token out of the command line

```toml
[auth."huggingface.co"]
provider = "env"
env = "HF_TOKEN"

[auth."private.example.com"]
provider = "command"
command = "pass show example/token"
ttl = 300
```

```bash
downctl auth check https://huggingface.co/meta-llama/Llama-3-8B
downctl get https://private.example.com/model.bin -d /data
```

`auth check` prints the matched rule and the header **names**, never the value.
A provider error exits `1`; `doctor` lists the configured rules.

## Serve a directory, then fetch it back

```bash
# machine A
downctl serve ~/models --bind 127.0.0.1 --port 8080

# machine B
downctl get http://127.0.0.1:8080/llama/model.safetensors -d ./copy
downctl verify ./copy
```

The server answers `206` for a single range, honours `If-Range`, sends `416`
when the range is unsatisfiable, and derives its ETag from the completion
marker, so resume works across the hand-off. Add `--auth user:pass` when the
bind is not loopback. Stop it with `Ctrl+C`.

## Measure or notify a run

```toml
[core]
hooks = ["metrics", "notify"]

[plugins.metrics]
path = "/var/lib/node_exporter/textfile/downctl.prom"

[plugins.notify]
url = "https://ntfy.sh/my-downctl"
only_failures = true
```

## Batch from stdin

```bash
printf '%s\n' \
  https://example.com/a.bin \
  https://example.com/b.bin | downctl get -l - -d ~/Downloads
```

## CI: cache a model, tolerate a flaky host

```bash
downctl get hf://org/model -d "$CACHE" \
  --skip-existing --retries 8 --stall 60 -j 2
```

## Diagnose a slow or stuck download

```bash
downctl -v get <url>     # resolved tasks, mirror rewrites, resume bytes
downctl doctor           # proxy / WSL / deps / plugins
downctl status <dir>     # what is incomplete or failed
```

If a host keeps stalling, lower `--stall`. If it is slow to warm up, raise it
and lower `--connections`.
