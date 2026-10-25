# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![Docs](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml/badge.svg)](https://thzxxart.github.io/downctl/)
[![coverage](https://thzxxart.github.io/downctl/coverage.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ThzxxArt/downctl/blob/master/LICENSE)

**English** | [简体中文](https://github.com/ThzxxArt/downctl/blob/master/README.md)

> A plugin-based universal command-line downloader with resumable,
> multi-connection accelerated and self-healing downloads.

**Everything is a plugin.**

`downctl` downloads files reliably from almost anywhere. Resumable piece
bitmaps, multi-connection acceleration, stall detection and automatic retries
are built into the stable core; everything else — sources, transports,
verifiers, reporters, routers, lifecycle hooks, job sources, auth providers and
servers — is a plugin, and the built-in functionality uses the exact same
plugin interface.

> Status: **pre-1.0**. The download core (resumable, multi-connection) and the
> CLI work end to end. See [`docs/design.md`](https://github.com/ThzxxArt/downctl/blob/master/docs/design.md) for the design.

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

Upgrading from 0.1.3: resume ledgers (`.downctl-pieces`) and completion markers
(`.downctl-done`) now also record the object's version witness. The format
versions are deliberately unchanged (`BITMAP_VERSION` stays `4`,
`COMPLETION_VERSION` stays `2`; the field is purely additive), so **downgrading
to 0.1.3 is safe**: the older build ignores the extra field and truncates
nothing. A ledger written by 0.1.3 carries no witness, so when the server names a
version (the normal case) it is discarded and an *incomplete* multi-connection
download restarts once; when the server offers no witness at all there is nothing
to compare and the download resumes exactly as 0.1.3 did. Completed files have no
bitmap and are untouched. A 0.1.3-era marker has no witness either and keeps being
trusted, so there is no mass re-download; the next run that takes the target to
completion rewrites it with the witness observed then. A witness-less marker that
is only *skipped* again (`--skip-existing`) is reported as `unversioned` and stays
witness-less on purpose — stamping today's witness onto a file fetched under an
unknown one would hide the next replacement
(`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`).
The `downctl.sinks`
entry-point group is retired — a package that still declares it gets a warning,
and `downctl plugin list` no longer shows a `sink` line (`API_VERSION` stays
`"1"`, so every other category that existed in 0.1.4 — six then, nine today —
keeps working). Retries are finite now
(`[core] retries = 10`); set `-r 0` / `retries = 0` for the old behaviour, and
note that probing stays bounded either way.

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

# Prove (offline) that already-completed files still match their source
downctl verify ~/Downloads

# Run several groups of targets, each with its own options
downctl batch jobs.toml

# Inspect credential resolution / hand files to another machine
downctl auth check https://huggingface.co/meta-llama/Llama-3-8B
downctl serve ~/models --port 8080
```

## Progress display

On a terminal the default `live` reporter draws an aggregate bar and one row
per working file, with the current rate, ETA, retry waits and the verifying
phase. The block is drawn on stderr, so `downctl get … > result.log` still
shows it on the terminal; the final summary stays on stdout.

```bash
downctl get <url> --progress live    # force the block (a pipe falls back to lines)
downctl get <url> --progress plain   # CI logs: periodic lines, no ANSI
downctl get <url> --progress off     # silent
downctl get <url> --reporter tqdm    # the 0.1.6 bars
```

```
⠼ downloading · 3 files · 34.7 MB / 36.0 MB · 1.9 MB/s · ETA 00:01
[██████████████████████████████████████▌░]  96%  0/3 done · 3 active
  ↻ file3.bin  retrying in 5s (attempt 2, HTTP 502)
  ▶ file1.bin  11.6 MB/12.0 MB   96%  668.9 KB/s  ETA 00:01
  ▶ file2.bin  11.4 MB/12.0 MB   95%  646.6 KB/s  ETA 00:01
```

`auto` keeps the 0.1.6 contract on a pipe (no progress chatter). `NO_COLOR`
disables colour only; `TERM=dumb` and a pipe disable the block. Unknown sizes
print `--%`/`--:--` instead of inventing a percentage or an ETA.

## What's new in 0.1.7

"Display truth." The download pipeline, state formats and `API_VERSION` are
unchanged. The default terminal reporter is now `live` (one bar + one row per
file, retry/verify states), `--progress auto|live|plain|off` and
`[core] progress` select the mode, and `[plugins.live]` configures fps,
Unicode, colour and the plain interval. `NO_COLOR` now only removes colour; it
no longer swaps the reporter for `quiet`. The run-level resume seed obeys
`--require-witness`, and the legacy tqdm reporter no longer leaks a bar across
`batch` jobs. `--reporter tqdm`, `json` and `quiet`, the JSON record names and
the `-q` result contract are unchanged.

## What's new in 0.1.6

Three new plugin categories (nine in total) and the commands that use them,
with no state-format migration (`BITMAP_VERSION` stays `4`,
`COMPLETION_VERSION` stays `2`, the failed log appends a v6 that old records and
readers still handle, and `API_VERSION` stays `"1"`).

- `downctl batch <file>` runs TOML or JSON job files (`--job`, `--job-source`,
  `--dry-run`, `--continue`, all `get` options as defaults). Exit `0` all
  succeeded, `1` a target/job failed, `2` the job file is invalid (nothing
  downloaded). See [`docs/batch.md`](https://github.com/ThzxxArt/downctl/blob/master/docs/batch.md).
- `downctl auth check <url>...` and `[auth."<host>"]` rules with `env`,
  `command` and `netrc` providers. Material is attached only to the first
  origin of a request; `-H`/`-u` wins; errors never print the value.
- `downctl serve <dir|file>` (`--bind`, `--port`, `--auth`, `--index`,
  `--quiet-logs`) serves with GET/HEAD, single-range 206, `If-Range`, 416, an
  ETag from the completion marker, Basic auth and path containment.
- `downctl plugin check <module-or-dist>` and `plugin new <name> --kind KIND`,
  plus the public `downctl.plugin.testing` helpers.
- New built-ins: `http_index`, `metrics`, `notify`, the `toml`/`json` job
  sources, `env`/`command`/`netrc` auth providers and the `http` server.

Behaviour changes: `--no-proxy`, `--limit-rate 0` = unlimited, `status` exits
`1` on failed/blocked targets with structured JSON `failed` entries, `verify`
exits `3` when unprovable, `-q` keeps the command's result, `config show` is
the effective config and masks `[auth]`, failed-log v6 refuses a retry whose
headers are no longer supplied, `--require-witness` refuses a full-size
hole-free unmarked file, and the manifest hook merges instead of overwriting.
Built-in plugins are registered by code only and no longer appear as entry
points.

## What's new in 0.1.5

Three defaults changed, each a fix:

- The HuggingFace token is **not** sent to a configured `mirror` unless
  `[plugins.huggingface] token_to_mirror = true`. Downloads from
  `huggingface.co` itself are unchanged.
- A *guessed* WSL host proxy applies only to remote targets; loopback,
  link-local and `no_proxy` matches connect directly. An explicit `-p`, a
  configured proxy or an environment proxy keeps its full reach.
- `checksum` and `manifest` write no artifact when the finish event reports
  `witness = none`, because both artifacts are fed back into `--checksums`.

Also: `probe --json` reports `validator_kind` (not `witness`); state written by
a newer downctl is reported and left untouched (`--force` is the explicit
override); and hook failures stay visible under `-q`.

## Resume guarantees

These guarantees rest on evidence: a **version witness** (a strong or weak
`ETag`, falling back to `Last-Modified`) or a checksum. When a server offers
neither, a replaced object can only cause a *stale skip* — never a spliced or
corrupt file — and `--require-witness` or `downctl verify --checksums` is how
you demand proof.

- A piece bitmap is checkpointed every 30 seconds (`--checkpoint N`; `0` =
  only on exit). With the default (`--checkpoint > 0`), `SIGKILL` or a power
  loss loses at most one interval; with `--checkpoint 0`, a hard kill loses this
  run's progress. Checkpointing applies to size-known, multi-connection
  downloads; unknown-size or non-Range servers use a single stream with no
  bitmap and restart from zero.
  `Ctrl+C` and `SIGTERM` (POSIX) shut down gracefully and snapshot immediately
  (Windows console processes have no catchable `SIGTERM`).
- Every completion writes `<file>.downctl-done` (size + mtime + URL identity +
  the version witness, plus the verified digest when one was used).
  A later run uses it to skip a **size-known** file without trusting its size;
  a file that was modified externally invalidates the marker. Unknown-size /
  non-Range single-stream downloads have no bitmap: re-running them starts
  over unless `--skip-existing` is passed, which can skip by the marker.
- Bitmaps and markers are tied to a canonical URL identity, and since 0.1.4 to
  the object's version witness as well. Switching a revision, mirror target or
  query-signed object starts a clean ledger instead of mixing pieces from two
  objects — and a URL whose *content* was replaced is detected too (see "What
  happens when the object is replaced" below).
- `--skip-existing` only skips when completion is *provable* (marker, complete
  bitmap, or a provably hole-free file), re-verifies a supplied checksum, and
  names the witness it used: `validator`, `checksum` or `none`. A skip with no
  witness prints an `unversioned` warning; `--require-witness` refuses it, and
  `--recheck` re-hashes existing files against their recorded/source digest
  before trusting them (it implies `--require-witness`).
- Retries are finite by default (`[core] retries = 10`). `-r 0` means "retry
  forever", but the resolve/probe stage stays bounded either way: at most 3
  attempts, with a 5 s cap on both the backoff and any `Retry-After` (a probe
  must not park a whole batch for the hour a server asks for). The transfer path
  honours `Retry-After` up to 300 s. `--max-time SECONDS` caps a single target's
  wall clock.
- `--probe-jobs N` (default 8, `0` = follow `--jobs`) expands and probes targets
  with bounded concurrency while failure reporting keeps command-line order.
- Before a run starts, the whole batch is budgeted against the filesystems it
  will be written to (plus a 200 MiB reserve): a batch that cannot fit fails up
  front with the need and the free space, without writing a byte.
- `--retry-failed` stores the original target, so S3 presigned URLs and private
  repositories are re-resolved with a fresh signature/token on retry.
- `--reporter json` keeps stdout pure JSON; warnings/errors stay on stderr.
- `probe` exits `1` when any target is unreachable, not HTTP(S), or answers
  with HTTP >= 400. `downctl verify` exits `0` (everything checked was proven),
  `1` (a mismatch or a marker without its data file) or `3` (nothing wrong, but
  nothing provable; `2` is reserved for argparse usage errors).

## What happens when the object is replaced

A resume ledger (`.downctl-pieces`, format v4) and a completion marker
(`<file>.downctl-done`, format v2) both record the remote object's **version
witness**: a strong `ETag`, a weak `ETag`, or the `Last-Modified` fallback —
and "none" when the server offers neither. The URL identity answers "same
address"; the witness answers "same bytes". Both are needed: a re-uploaded
release asset keeps its URL while its content changes.

| Case | What downctl does |
|---|---|
| Same version (witness matches) | Resumes: only missing pieces are fetched; a finished file is skipped by its marker |
| Replaced between runs | The ledger's witness no longer matches → the ledger is discarded and the object is **fetched again from zero**; a completion marker is refused too, so `--skip-existing` cannot bless stale content |
| Replaced during a run | Range requests carry `If-Range`; when the server answers a conditional request with a full body, one extra probe tells "no range support" (fall back to a single stream) from "the object was replaced" (**one restart from zero**, `ContentChangedError`; a second occurrence fails the target) |
| Server offers no witness | Nothing can be compared: a finished file may still be skipped, but the run says so (`unversioned`, `"witness": "none"` in the hook payload and dry-run JSON); `--require-witness` refuses that skip and that resume |

The regression suite:
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`,
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`,
`tests/integration/test_content_truth.py::test_cli_run_does_not_splice_a_replaced_object`,
`tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`
and
`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`.
A server that renames its object on every response is detected and that target
continues without a witness (reported as unversioned) instead of restarting
forever:
`tests/integration/test_content_truth.py::test_unstable_etag_still_downloads`. A
weak `ETag` is used for comparison but never sent in `If-Range`:
`tests/integration/test_content_truth.py::test_normalize_validator_never_sends_a_weak_etag_conditionally`.

Offline, `downctl verify [DIR]` re-examines every file that claims completion and
reports `ok` / `mismatch` / `unverifiable` / `missing` together with the witness
it used. `--checksums FILE` turns `unverifiable` into proof wherever the manifest
has an entry; `--repair` deletes only what was **proven** bad (plus orphan
markers) and is destructive.

## Docker

```bash
docker build -t downctl .
docker run --rm -v "$PWD:/data" downctl get <target>
```

## Plugins

| Category | Purpose | Built-ins |
|---|---|---|
| Source | Resolve input into tasks | `url`, `http_index`, `huggingface`, `github`, `s3`, `sftp`, `file` |
| Transport | Transfer bytes | `http`, `sftp`, `file` |
| Verifier | Check digests | `sha256`, `sha1`, `sha512`, `md5` |
| Reporter | Progress & logs | `tqdm`, `json`, `quiet` |
| Router | Rewrite URLs | `mirror` |
| Hook | Lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum`, `metrics`, `notify` |
| Job source | Read a batch file | `toml`, `json` |
| Auth provider | Credentials for a host | `env`, `command`, `netrc` |
| Server | Hand files to another machine | `http` |

Plugins are discovered from three places: built-ins, installed packages
advertising one of the nine `downctl.*` entry-point groups, and local
directories (`./downctl_plugins`, `$XDG_CONFIG_HOME/downctl/plugins`; the
latter only with `--local-plugins`). Built-ins are registered by code only and
no longer appear in `metadata.entry_points`.

The `sink` category was removed in 0.1.4: it was never called on the write path
(the core always wrote through `core/transfer.py`), so it promised a replaceable
destination that never took effect. The `downctl.sinks` entry-point group is
retired — a package that still declares it gets a warning from the loader
instead of being ignored silently — and `downctl plugin list` no longer shows a
`sink` line. `API_VERSION` stays `"1"`, so plugins in the other categories keep
working.

An entry point may be a class, an instance or a zero-argument factory; the
plugin's own `name` is the registry key. Use `downctl plugin new` to scaffold a
working package and `downctl plugin check` to run the same contract checks the
registry applies (`downctl.plugin.testing` exposes them to your tests).

### Write a plugin

See [`examples/plugins/`](https://github.com/ThzxxArt/downctl/tree/master/examples/plugins) for a minimal installable plugin
package. A plugin is any object exposing the metadata (`name`, `api_version`,
`priority`) and the methods of one of the contracts in
[`downctl.plugin.protocols`](https://github.com/ThzxxArt/downctl/blob/master/src/downctl/plugin/protocols.py).

> **Security:** local-directory plugins execute arbitrary code. Only enable
> directories you trust; entry-point plugins are loaded automatically.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,sftp]'
ruff check . && ruff format --check . && mypy
pytest --cov=downctl --cov-fail-under=85
# Per-module gates for the state machine (CI runs each one):
coverage report --include="*/downctl/core/pieces.py"   --fail-under=95
coverage report --include="*/downctl/core/transfer.py" --fail-under=95
coverage report --include="*/downctl/core/engine.py"   --fail-under=95
```

A few tests start real child processes (`SIGTERM`, `kill -9`) and compare a
SHA-256 of the final file against the server's current revision. They are slower
than the rest of the suite on purpose: they are what makes "never silently
corrupt" a falsifiable claim.

## Documentation

The full, detailed guide (install, all options, sources, configuration,
plugins, troubleshooting) is the [Chinese README](https://github.com/ThzxxArt/downctl/blob/master/README.md); the docs site is
bilingual.

- Docs site: https://thzxxart.github.io/downctl/ (EN / 简体中文)
- [CLI reference](https://github.com/ThzxxArt/downctl/blob/master/docs/cli.md)
- [Plugin development](https://github.com/ThzxxArt/downctl/blob/master/docs/plugins.md)
- [Design](https://github.com/ThzxxArt/downctl/blob/master/docs/design.md)
- [Contributing](https://github.com/ThzxxArt/downctl/blob/master/CONTRIBUTING.md) · [Security](https://github.com/ThzxxArt/downctl/blob/master/SECURITY.md) · [Releasing](https://github.com/ThzxxArt/downctl/blob/master/RELEASING.md)

## License

[MIT](https://github.com/ThzxxArt/downctl/blob/master/LICENSE) © ThzxxArt
