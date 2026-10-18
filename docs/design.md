# Design

> A plugin-based universal command-line downloader.
> Resumable, multi-connection accelerated, self-healing — everything is a plugin.

- **Package / command**: `downctl`
- **Repository**: https://github.com/ThzxxArt/downctl
- **License**: MIT · **Python**: 3.11+

---

## 1. Positioning

`downctl` downloads files reliably from almost anywhere. It is not tied to a
single site: HuggingFace, GitHub Releases, S3, SFTP and local files are all
just built-in **source plugins**, using exactly the same interface as
third-party plugins.

**Everything is a plugin.**

---

## 2. The core/plugin boundary

"Everything is a plugin" needs to distinguish two kinds of thing, otherwise a
stateful, tightly-coupled algorithm gets shattered into slow, fragile pieces.

### 2.1 The plugin layer (replaceable)

All strategy lives behind one interface, and the built-ins use that same
interface (dogfooding):

| Category | Responsibility | Built-ins |
|---|---|---|
| Source | Resolve input into tasks | `url`, `huggingface`, `github`, `s3`, `sftp`, `file` |
| Transport | Transfer bytes | `http`, `sftp`, `file` |
| Verifier | Check digests | `sha256`, `sha1`, `sha512`, `md5` |
| Reporter | Progress & logs | `tqdm`, `json`, `quiet` |
| Router | Rewrite URLs | `mirror` |
| Hook | Lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum` |

> The **Sink** category was removed in 0.1.4. It was never called on the write
> path — the core has always written ranged bytes through `core/transfer.py` —
> so the contract promised a replaceable destination that never took effect.
> The `downctl.sinks` entry-point group is retired: the loader warns when an
> installed package still declares it instead of ignoring it silently, and
> `API_VERSION` stays `"1"`, so plugins in the other six categories keep
> working.

### 2.2 The core (stable, not a plugin)

Stateful algorithms stay in a stable core, exposed to plugins as API:

- scheduling and concurrency control
- the resumable **piece bitmap**
- the **stall watchdog**
- rate limiting
- file locking
- disk-space pre-flight

> The core is the engine; the plugins are the gears. We don't turn the engine
> into gears.

---

## 3. Layout

```
downctl/
├── src/downctl/
│   ├── cli.py, app.py, config.py, session.py, probe.py, console.py,
│   │   platform_utils.py, urls.py (display/redaction + URL identity)
│   ├── core/            # stable engine (not plugins)
│   │   ├── engine.py transfer.py pieces.py disk.py watchdog.py
│   │   ├── ratelimit.py locks.py task.py context.py errors.py
│   ├── plugin/          # registry + loader + event bus + protocols
│   └── builtin/         # built-in plugins (via the plugin interface)
│       ├── sources/{url,huggingface,github,s3,sftp,file}, _sigv4
│       ├── transports/{http,sftp,file}, _sftp
│       ├── verifiers/{sha256,digest}_verifier.py
│       ├── reporters/{tqdm,json,quiet}_reporter.py
│       ├── routers/mirror_router.py
│       └── hooks/{logfile,archive,webhook,manifest,exec,checksum}_hook.py
├── tests/{unit, integration}/
├── docs/  (bilingual site)
├── examples/plugins/
└── .github/workflows/{ci, docs, release, codeql}.yml
```

---

## 4. Plugin system

### 4.1 Discovery (highest priority first)

1. Local directories: `./downctl_plugins/`, `~/.config/downctl/plugins/`
   (only loaded with `--local-plugins`; untrusted code)
2. Installed packages advertising a `downctl.*` entry-point
3. Built-ins: `downctl.builtin`

### 4.2 Selection

- Route by **URI scheme**: `https://`→`http`, `hf://`→`huggingface`,
  `gh://`→`github`, `s3://`→`s3`, `sftp://`→`sftp`, `file://`→`file`.
- `--source` forces a specific plugin.
- Several candidates are ranked by `priority` + `can_handle(target)`.

### 4.3 API versioning

`downctl.API_VERSION` (currently `"1"`). A plugin declares the version it was
built against; the registry rejects mismatches.

### 4.4 Transports carry the protocol

A transport returns a streaming `Response` (status, headers, chunk iterator)
and maps its native errors onto the downctl exception hierarchy, so the core
never depends on a specific HTTP library.

---

## 5. Resumable downloads

- A download is split into ~8 MiB pieces; a single file uses up to `connections`
  in parallel.
- A **piece bitmap** (`<file>.downctl-pieces`) is the authoritative ledger of
  completed pieces. Multi-connection completion is out of order, so file size
  alone cannot describe resume state — the bitmap can.
- The ledger is bound to a **URL identity** (SHA-256 of the canonical URL;
  a rotating presigned query is excluded). A bitmap for another URL — or a
  pre-v4 bitmap that has no identity — is discarded instead of being resumed
  into a different object.
- The ledger is *also* bound to the object's **version witness**, added in 0.1.4
  as an additive field — the ledger format version deliberately stays `4` so a
  downgrade to 0.1.3 ignores it instead of truncating the file: the strong
  `ETag` the server named, a weak one, or the `Last-Modified` fallback; a server
  that offers none leaves it empty ("unversioned"). A ledger whose witness
  differs from the one just observed is discarded, never spliced into the
  current revision. `urls.normalize_validator` owns that normalisation — a weak
  `ETag` remains a usable comparison key but never an `If-Range` value, because
  RFC 9110 requires a strong validator for a conditional range request.
- **Every** HTTP(S) target is probed once for that witness, including targets
  whose size the source already reported (an S3 listing, a HuggingFace API
  response, a GitHub asset). The probe is what makes the transfer conditional and
  the completion claim checkable; skipping it for a known size left the transfer
  sending unconditional ranges and produced a marker that could never notice a
  later replacement. One `HEAD` per target is the entire cost. The probe sends
  `Accept-Encoding: identity` to match the transport, and falls back to a ranged
  `GET` when `HEAD` is unusable — including a method-scoped `401`/`403`, which is
  what a GET-signed presigned URL answers to `HEAD` while serving the ranged
  `GET` the transfer uses just fine.
- Ranged requests are **conditional** (`If-Range`) whenever a strong witness is
  known, and every `206` must answer with a `Content-Range` covering exactly the
  requested window — a response for another window is refused before a byte is
  written. A full body (`200`) answering a conditional request is resolved with
  one extra probe: either the server cannot do ranges (single-stream fallback)
  or the object was replaced (`ContentChangedError`), which the engine turns
  into one restart from zero.
- On interruption the data file is `fsync`-ed, then the bitmap is written
  atomically; a retry re-fetches only missing pieces. While a transfer runs,
  the bitmap is checkpointed every `checkpoint` seconds (default 30), so a
  hard kill loses at most one interval. The atomic rename is followed by a real
  directory `fsync`: 0.1.4 fixed `pieces._fsync_dir`, which opened the directory
  with `open(dir, "rb")` and therefore never fsynced anything on POSIX (the
  `IsADirectoryError` was silently swallowed).
- The bitmap records its piece size (v3), so changing `--connections` /
  `--piece-size` **reslices** completion state by byte range instead of
  discarding progress.
- Completion writes `<file>.downctl-done` (size + mtime + URL identity + the
  version witness, also added additively: the marker format stays `2`, and a
  0.1.3-era marker without a witness keeps being trusted as `unversioned`). The
  engine only skips an existing file when this
  marker, a complete bitmap or a provably hole-free file proves it — never from
  size alone. A marker is invalidated by an external modification. Every skip
  names its witness — `validator`, `checksum` or `none` — so it is never
  presented as blanket proof: with no witness the run warns that the skip is
  `unversioned`, and `require_witness` refuses it.
- Reporters can seed a resume-aware bar: the engine computes already-complete
  bytes from the marker/bitmap and calls the optional `run_resumed` /
  `task_resumed` hooks.
- Without a marker, a full-size file is trusted only if it provably has no
  holes (SEEK_HOLE) or the user opts in with `DOWNCTL_TRUST_PARTIAL=1`. WSL
  DrvFS and Windows distrust marker-less full-size files on purpose.

### 5.1 Failure recovery

The failed log stores the resolved URL, target, expected checksum/size, source
plugin, and — since v3 — the **origin** the user asked for. `--retry-failed`
re-resolves the origin, refreshing S3 presigned signatures and private-repo
tokens, then matches the task by target/URL basename. Records without an origin
still load, with a warning when the stored URL may have expired.

### 5.2 What happens when the object is replaced

The URL identity answers "same address"; the version witness answers "same
bytes". Both are needed, because a re-uploaded release asset or a rewritten
model shard keeps its URL while its content changes. The two records use
deliberately different rules: a **ledger** is a resume record, so a witness
mismatch discards it (reusing those pieces is exactly what splices two
revisions together); a **completion marker** is a completion record, so a
comparison is only possible when both sides carry a witness. The worst case of
the weaker marker rule is a *stale skip*, never a spliced file — and being
stricter would re-download everything whenever a server starts or stops sending
ETags. `--require-witness` opts into the strict rule for both. A skip is not
re-stamped with the witness observed that day unless the proof that allowed it
*was* a witness (`validator`) or a verified digest (`checksum`): a marker written
before the witness existed — no validator, no digest — keeps being trusted
(upgrading must not force a mass re-download) and only gains the witness when a
later run takes the target to completion
(`tests/integration/test_content_truth.py::test_a_legacy_marker_keeps_being_trusted`).

| Case | Behaviour |
|---|---|
| Same version (witness matches) | Resume: only missing pieces are fetched; a finished file is skipped by its marker |
| Replaced between runs | The ledger's witness differs → ledger discarded, object **fetched again from zero**; the marker is refused too, so `--skip-existing` cannot bless stale content |
| Replaced during a run | `If-Range` no longer matches → `ContentChangedError` → **one restart from zero** (a second occurrence fails the target) |
| Server offers no witness | Nothing can be compared; a skip is allowed but reported as `unversioned` (`"witness": "none"`), and `--require-witness` refuses it |

The regression suite for these four cases lives in
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`,
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`,
`tests/integration/test_content_truth.py::test_cli_run_does_not_splice_a_replaced_object`,
`tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`
and
`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`.
A weak `ETag` is compared but never sent in `If-Range`
(`tests/integration/test_content_truth.py::test_normalize_validator_never_sends_a_weak_etag_conditionally`),
and a server that renames its object on every response is detected — that target
continues without a witness instead of restarting forever
(`tests/integration/test_content_truth.py::test_unstable_etag_still_downloads`).

Offline proof is a separate channel: `downctl verify [DIR]` judges every file
that carries a recognized completion marker and reports `ok` / `mismatch` /
`unverifiable` / `missing` together with the witness it used, exiting `0` (all
proven), `1` (a mismatch or a marker without its data file) or `2` (nothing
wrong, nothing provable). `--checksums FILE` supplies the digest that turns
`unverifiable` into proof; `--repair` deletes only what was proven bad.

---

## 6. CLI

```
downctl get <target> [<target>...]    # download; source auto-selected by scheme
    -d/--dir DIR          destination directory
    -o/--output PATH      save a single download to PATH
    -j/--jobs N           concurrent files
    --connections N       per-file connections (default 16)
    --stall SECONDS       stall watchdog (default 30)
    --checkpoint SECONDS  durable resume snapshot interval (default 30)
    --piece-size SIZE     piece size, e.g. 8M
    --min-split-size SIZE files smaller than this use one connection
    -t/--limit-rate RATE  rate limit (5M / 1m / 512k)
    -r/--retries N        max retries per file (default 10; 0 = retry forever)
    --max-time SECONDS    wall-clock cap for one target (0 = no limit)
    --probe-jobs N        concurrent resolve/probe (default 8; 0 = follow --jobs)
    --force / --skip-existing   re-download / skip only what is provable
    --require-witness     refuse to skip or resume state with no checkable version
    --recheck             re-hash existing files before skipping
    --checksum ALGO:HEX   verify a single target after download
    --checksums FILE      verify against a *sum-style manifest
    -p/--proxy URL        proxy
    -H/--header K:V       extra request header (repeatable)
    -u/--user USER:PASS   HTTP Basic auth
    --mirror FROM=TO      URL prefix rewrite
    --source NAME         force a source plugin
    --reporter NAME       progress reporter
    --hook NAME           enable a hook for this run (repeatable)
    --user-agent UA       custom HTTP User-Agent
    --dry-run             probe only, do not download
    --retry-failed        retry the failed log
    -l/--list FILE        read targets from a file (- = stdin)
    --continue            keep going when a target cannot be resolved/probed

downctl probe <target> [--json]       # size/status/witness only, no download
downctl status [DIR] [--json]         # incomplete/failed downloads
downctl verify [DIR] [--json] [--checksums FILE] [--repair]
                                      # prove completed files offline (exit 0/1/2)
downctl clean [DIR] [--dry-run] [--json]  # remove leftover bitmaps / failed log / locks
downctl plugin list|info <name>       # plugin management
downctl config init|show|path         # configuration management
downctl doctor                        # environment diagnostic
downctl wizard                        # interactive
downctl completions bash|zsh|fish     # shell completion script
downctl version
```

---

## 7. Configuration

`~/.config/downctl/config.toml` (override with `DOWNCTL_CONFIG`), with
`${VAR}` interpolation. Plugin options live under `[plugins.<name>]`.

See [Configuration](configuration.md) for the full key list.

---

## 8. Packaging & CI

- **Build backend**: hatchling (PEP 621), version dynamic from
  `src/downctl/__init__.py`.
- **CI**: lint (ruff/mypy) + matrix tests (ubuntu/macos/windows × 3.11–3.14,
  coverage ≥ 85%) + build/twine-check + `pip-audit`. Three modules carry their
  own 95% gate — `core/pieces.py`, `core/transfer.py`, `core/engine.py` —
  because a healthy global percentage hid the state machine that decides
  whether a file counts as finished.
- **Tests that can falsify the promise**: the suite starts real
  `python -m downctl` processes (`SIGTERM` exits 130 with progress kept,
  `kill -9` followed by a restart resumes only the missing pieces —
  `tests/integration/test_cli_process.py::test_sigterm_exits_130_and_keeps_resumable_progress`,
  `tests/integration/test_cli_process.py::test_hard_kill_then_restart_resumes_only_missing_pieces`)
  and checks content invariants by comparing a SHA-256 of the final file against
  the server's current revision, not a piece flag or an exit code.
- **Release**: `release.yml` on `v*` tags publishes to PyPI via Trusted
  Publishing (OIDC) and creates a GitHub Release.
- **Docs**: `docs.yml` builds this site and deploys to GitHub Pages.

---

## 9. Roadmap

Done: resumable core, plugin framework, url/hf/github/s3/sftp/file sources,
http/sftp/file transports, hooks, CLI, bilingual docs, Docker, PyPI release.

0.1.2 (polish): resume-aware progress, resliceable bitmaps, `--force` /
`--skip-existing`, stdin targets, `--piece-size` / `--min-split-size`,
`probe --json`, `completions`, `exec` / `checksum` hooks, GitHub private-asset
downloads, richer verbose output.

0.1.3 (state truth): URL-bound bitmaps (v4) and completion markers,
periodic durable checkpoints, proof-based `--skip-existing`, origin-based
`--retry-failed`, credential redaction across logs/JSON/webhooks/exec,
honest JSON/stdout separation, local-plugin opt-in, Python 3.14 CI.

0.1.4 (content truth): resume ledgers and completion markers record the object's
version witness (an additive field — `BITMAP_VERSION` stays `4` and
`COMPLETION_VERSION` stays `2`, so downgrading to 0.1.3 stays safe), conditional
range requests (`If-Range`) with
`Content-Range` validation, `downctl verify` as the offline proof/repair
channel, `--require-witness` / `--recheck`, probe retry and error
classification with `Retry-After`, finite retries by default, `--max-time`,
`--probe-jobs`, the per-filesystem batch disk budget, the `sink` retirement and
the `pieces._fsync_dir` durability fix.

Optional next: richer reporter (rich), Homebrew / standalone binaries,
additional sources.
