# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![Docs](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml/badge.svg)](https://thzxxart.github.io/downctl/)
[![coverage](https://thzxxart.github.io/downctl/coverage.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** | [简体中文](README.md)

> A plugin-based universal command-line downloader with resumable,
> multi-connection accelerated and self-healing downloads.

**Everything is a plugin.**

`downctl` downloads files reliably from almost anywhere. Resumable piece
bitmaps, multi-connection acceleration, stall detection and automatic retries
are built into the stable core; everything else — sources, transports,
verifiers, sinks, reporters, routers and lifecycle hooks — is a plugin, and the
built-in functionality uses the exact same plugin interface.

> Status: **pre-1.0**. The download core (resumable, multi-connection) and the
> CLI work end to end. See [`docs/design.md`](docs/design.md) for the design.

## Install

```bash
pip install downctl
```

Requires Python 3.11+.

## Upgrade

```bash
python -m pip install --upgrade downctl          # pip
python -m pip install --upgrade 'downctl[sftp]'  # with optional extras

pipx upgrade downctl                              # pipx
pipx install --force 'downctl[sftp]'              # add extras

downctl version                                   # verify
```

Upgrading does not delete your downloads, `~/.config/downctl/config.toml`, the
failed log, or completion markers. Config keys are backward compatible
(unknown keys are ignored).

Resume state is bound to a URL identity since 0.1.3: bitmaps written by 0.1.2 or
older cannot prove which URL they belong to and are discarded on first resume.
Completed files have no bitmap, so the v4 change itself does not touch them.
On Windows / WSL DrvFS, a full-size file without a marker is no longer trusted
blindly: **files completed by 0.1.2 or older have no `<file>.downctl-done`
marker and are re-fetched once** on those filesystems (use
`DOWNCTL_TRUST_PARTIAL=1` only if you know the file is complete). On native
Linux/macOS the hole scan can prove the file complete and the marker is written
without re-downloading.

## Usage

```bash
# Download a file
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# Download a HuggingFace repository (built-in source plugin)
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# Probe without downloading
downctl probe --json https://example.com/big.iso

# Re-run safely (skip complete files) or force a fresh copy
downctl get https://example.com/big.iso --skip-existing
downctl get https://example.com/big.iso --force

# Shell completions
downctl completions bash >> ~/.bashrc

# Inspect plugins and environment
downctl plugin list
downctl doctor

# Show incomplete / failed downloads
downctl status ~/Downloads
```

## Resume guarantees

- A piece bitmap is checkpointed every 30 seconds (`--checkpoint N`, `0` =
  only on exit), so `SIGKILL` or a power loss loses at most one interval.
  Checkpointing applies to size-known, multi-connection downloads; unknown-size
  or non-Range servers use a single stream with no bitmap and restart from
  zero.
  `Ctrl+C` and `SIGTERM` (POSIX) shut down gracefully and snapshot immediately
  (Windows console processes have no catchable `SIGTERM`).
- Every completion writes `<file>.downctl-done` (size + mtime + URL identity,
  plus the verified digest when one was used).
  A later run uses it to skip a **size-known** file without trusting its size;
  a file that was modified externally invalidates the marker. Unknown-size /
  non-Range single-stream downloads have no bitmap: re-running them starts
  over unless `--skip-existing` is passed, which can skip by the marker.
- Bitmaps and markers are tied to a canonical URL identity. Switching a
  revision, mirror target or query-signed object starts a clean ledger instead
  of mixing pieces from two objects.
- `--skip-existing` only skips when completion is *provable* (marker, complete
  bitmap, or a provably hole-free file) and re-verifies a supplied checksum.
- `--retry-failed` stores the original target, so S3 presigned URLs and private
  repositories are re-resolved with a fresh signature/token on retry.
- `--reporter json` keeps stdout pure JSON; warnings/errors stay on stderr.
- `probe` exits `1` when any target is unreachable, not HTTP(S), or answers
  with HTTP >= 400.

## Docker

```bash
docker build -t downctl .
docker run --rm -v "$PWD:/data" downctl get <target>
```

## Plugins

| Category | Purpose | Built-ins |
|---|---|---|
| Source | Resolve input into tasks | `url`, `huggingface`, `github`, `s3`, `sftp`, `file` |
| Transport | Transfer bytes | `http`, `sftp`, `file` |
| Verifier | Check digests | `sha256`, `sha1`, `sha512`, `md5` |
| Sink | Persist bytes | `filesystem` |
| Reporter | Progress & logs | `tqdm`, `json`, `quiet` |
| Router | Rewrite URLs | `mirror` |
| Hook | Lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum` |

Plugins are discovered from three places: built-ins, installed packages
advertising `downctl.*` entry-points, and local directories
(`./downctl_plugins`, `~/.config/downctl/plugins`) — the latter only with
`--local-plugins`.

### Write a plugin

See [`examples/plugins/`](examples/plugins) for a minimal installable plugin
package. A plugin is any object exposing the metadata (`name`, `api_version`,
`priority`) and the methods of one of the contracts in
[`downctl.plugin.protocols`](src/downctl/plugin/protocols.py).

> **Security:** local-directory plugins execute arbitrary code. Only enable
> directories you trust; entry-point plugins are loaded automatically.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,sftp]'
ruff check .
mypy
pytest
```

## Documentation

The full, detailed guide (install, all options, sources, configuration,
plugins, troubleshooting) is the [Chinese README](README.md); the docs site is
bilingual.

- Docs site: https://thzxxart.github.io/downctl/ (EN / 简体中文)
- [CLI reference](docs/cli.md)
- [Plugin development](docs/plugins.md)
- [Design](docs/design.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Releasing](RELEASING.md)

## License

[MIT](LICENSE) © ThzxxArt
