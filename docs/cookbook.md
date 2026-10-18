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

A mismatch re-downloads (up to twice) before failing, and the file is recorded
in `.downctl.failed` for `--retry-failed`.

Later — or on another machine that has the files but not the origin — prove what
is on disk without touching the network:

```bash
downctl verify ~/models --checksums SHA256SUMS   # exit 0 = proven, 2 = unprovable
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

```bash
[core]
hooks = ["exec"]

[plugins.exec]
command = "notify-send 'downctl' \"$DOWNCTL_TARGET done\""
events = ["on_task_finish"]
```

See [`hooks.md`](hooks.md) for the full event/variable list.

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
