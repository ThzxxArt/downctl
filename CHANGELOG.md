# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet: `[0.1.4]` is the current release. Add entries here while working on
the next version, and move them under a version heading when it ships.

## [0.1.4] - 2026-10-18

"Content truth": a ledger now knows *which version* of an object it describes,
a completion claim without a witness is reported as unproven, and a replaced
object can no longer be spliced into a file that already holds another
revision's bytes.

### Fixed

- **Silent splicing of two revisions (the 0.1.3 defect).** Resuming a download
  whose URL stayed the same but whose content was replaced produced a file made
  of the old prefix and the new suffix, exited 0 and wrote a completion marker —
  sticky corruption that only `--force` could repair. It is now impossible:
  - resume ledgers (`.downctl-pieces`) record the object's version witness; a
    ledger whose witness differs from the one just observed is discarded instead
    of resumed;
  - ranged requests carry `If-Range` when a strong witness is known, so a
    replacement *during* a transfer turns into one clean restart;
  - a `206` whose `Content-Range` does not cover exactly the requested window is
    refused before a byte is written (previously the body was trusted blindly);
  - a server whose witness changes on every response is detected and that target
    continues without one (reported as unversioned) instead of restarting
    forever.
- **Every HTTP(S) target is probed for a witness, even when its size is already
  known.** Sources that report a size (an S3 listing, a HuggingFace API
  response, a GitHub asset) used to skip the probe entirely, which left the
  transfer unconditional and produced a marker that could never detect a later
  replacement — the same defect, still reachable, for exactly the large files
  that matter most. The cost is one `HEAD` per target.
- **Completion markers record the witness**, so `--skip-existing` refuses a file
  the server has since replaced instead of reporting stale content as proven.
  A marker written by 0.1.3 keeps being trusted (reported as `unversioned`) and
  gains the witness when the run actually *completes* the target, or when a skip
  was proven by a checksum — a plain unversioned skip deliberately keeps the
  marker witness-less, because stamping today's version onto a file fetched
  under an unknown one would hide the next replacement.
- **Probing no longer misclassifies transient failures.** `probe` bypassed the
  transport's error classification, so a single `500` during the size probe was
  reported as a permanent failure after two requests. `5xx`/`429`/`408`/`425`
  and network failures are now retried (bounded, with `Retry-After` honoured),
  and probing raises `ProbeError` only after the retries are exhausted.
- **`416` and content changes are recoverable.** An unsatisfiable range (the
  object shrank) used to be a permanent failure; the engine now re-probes the
  size, resets the target once and downloads the current object.
- **`pieces._fsync_dir` never fsynced anything.** It opened the directory with
  `open(dir, "rb")`, which raises `IsADirectoryError` on POSIX — swallowed by a
  `suppress(OSError)` — so the durability step that makes a bitmap rename
  crash-safe silently did nothing. It now uses `os.open(dir, O_RDONLY)`.
- `--skip-existing` on an object whose version cannot be checked says so: the
  run warns that the skip is `unversioned` and reports
  `"witness": "none"` in the hook payload and dry-run JSON.

### Added

- **`downctl verify [DIR] [--json] [--checksums FILE] [--repair]`** — the
  offline proof/repair channel: every file that claims completion is reported as
  `ok` / `mismatch` / `unverifiable` / `missing`, together with the witness that
  proved it. Exit codes: `0` proven, `1` mismatch or missing data file, `2`
  nothing wrong but nothing provable. `--repair` deletes only what was *proven*
  bad (plus orphan markers).
- `--require-witness` / `[core] require_witness`: refuse to skip or resume state
  whose object version cannot be checked.
- `--recheck`: re-hash existing files against their recorded or source digest
  before skipping (implies `--require-witness`).
- `--max-time SECONDS` / `[core] max_time`: wall-clock cap for a single target.
- `--probe-jobs N` / `[core] probe_jobs` (default 8): targets are now expanded
  and probed with bounded concurrency instead of one round trip after another,
  while failure reporting keeps the order the targets were given in.
- **Batch disk budget**: the whole run is pre-flighted against the filesystems it
  will write to (per device, plus the 200 MiB reserve). A batch that cannot fit
  fails before the first byte with one message naming the need and the free
  space, instead of dying with `ENOSPC` half-way through.
- `Retry-After` is honoured on both the probe and the transfer path (capped at
  300 s), and `downctl probe --json` reports the version witness.
- Per-module coverage gates in CI: `core/pieces.py`, `core/transfer.py` and
  `core/engine.py` must stay at or above 95% (a global 85% hid the state machine
  that decides whether a file counts as finished), and the subprocess tests now
  measure `python -m downctl` itself.

### Changed

- **Retries are finite by default** (`[core] retries = 10`); `-r 0` still means
  "retry forever", but the probe stage stays bounded either way. An unbounded
  retry loop as the default turned a permanently failing server into a silent CI
  hang.
- **The `sink` plugin category was removed.** It was never called on the write
  path (the core always wrote through `core/transfer.py`), so it promised a
  replaceable destination that never took effect. The `downctl.sinks`
  entry-point group is retired and the loader now warns when an installed
  package still declares it, instead of ignoring it silently. `API_VERSION`
  stays `"1"`: plugins in the other six categories are unaffected.
- `--skip-existing` treats a skip as one of three states — `validator`,
  `checksum` or `none` — instead of one blanket "completion proven".
- The test suite now holds real content invariants: it compares a SHA-256 of the
  final file against the server's current revision (not a piece flag or an exit
  code), and it starts real `python -m downctl` processes to test `SIGTERM`
  (exit 130 with progress kept) and `kill -9` followed by a resuming restart.

### Notes

- **No state-format version was bumped.** The `validator` field is additive in
  both the ledger and the marker, and older builds read the files they wrote
  without noticing it. That is deliberate: 0.1.3 refuses a marker whose version
  is newer than it knows and then truncates the *completed* file, so a version
  bump would have turned "downgrade to 0.1.3" into data loss. Downgrading is
  safe.
- Upgrading from 0.1.3: an **incomplete** ledger has no witness, so it restarts
  once whenever the server names a version (the normal case) and resumes as
  before when the server offers none. Completed files, markers and the failed
  log are untouched. See `docs/upgrading.md`.
- The failed-log format also stays as it is: the retry path always re-probes, so
  a witness stored there would only ever be stale evidence.

## [0.1.3] - 2026-10-17

"State truth": a ledger now belongs to exactly one remote object, completion
has to be provable, and failures can be retried without replaying expired
credentials.

### Added

- **URL-bound resume state (bitmap v4)**: every bitmap records a SHA-256 of the
  canonical URL (the rotating query of a presigned URL is excluded). A bitmap
  written for another URL — or by 0.1.2 or older, which has no identity — is
  discarded instead of being resumed into a different object.
- **Completion markers** (`<file>.downctl-done`, size + mtime + URL identity,
  plus the verified checksum when one was used): a finished size-known file is
  skipped without trusting its size, including on WSL DrvFS and Windows where
  holes cannot be detected. Unknown-size or non-Range single-stream downloads
  have no bitmap; `--skip-existing` can skip them by the marker. The marker is
  invalidated by an external modification; a changed source digest forces a
  re-hash instead of trusting a stale skip.
- **Periodic durable checkpoints** (`--checkpoint SECONDS`, `[core] checkpoint`,
  default 30, `0` disables): the data file is `fsync`-ed and the bitmap written
  atomically while a transfer runs, so `SIGKILL` or power loss loses at most one
  interval. `SIGTERM` is converted into a graceful interrupt.
- **Failure recovery by origin (failed log v3)**: records store the target the
  user asked for; `--retry-failed` re-resolves it, so S3 presigned URLs are
  re-signed and private HuggingFace/GitHub downloads get the current token.
- `downctl status --json` and `downctl clean --json`.
- `--reporter json` emits a `failure` record for targets that fail during
  resolve/probe, and `run_finish.failed` counts them (not just engine failures).
- Completion markers v2 also record the verified checksum, so a changed source
  digest no longer lets `--skip-existing` bless stale content.
- `clean` removes recognized orphaned completion markers whose data file no
  longer exists; a user data file that merely ends in `.downctl-done` is left
  alone (it is not a valid marker).
- Verifiers for `sha1`, `sha512` and `md5`, so every algorithm the `checksum`
  hook can write is consumable via `--checksums`. Unlabelled `*sum` files are
  matched by digest length (32/40/64/128 hex characters).
- `--checksums` fails closed: every requested target must have an entry, and
  an empty/unusable manifest is rejected before downloading.
- Resume identity is bound to the final fetched URL (after mirror routing), so
  switching a mirror invalidates old pieces/markers.
- Python 3.14 in the CI matrix and package classifiers.

### Fixed

- `--skip-existing` no longer blesses a full-size file with holes (an
  interrupted multi-connection download from an earlier run). It skips only
  when a marker, a complete bitmap or a provably hole-free file proves
  completion, and re-verifies an explicitly supplied checksum.
- `--skip-existing` also refuses a complete bitmap whose data file has been
  truncated (the ledger's durable high-water mark is checked) and refuses an
  oversized stale file instead of truncating an unproven prefix and calling it
  complete.
- A complete bitmap is only trusted when the file covers every claimed piece
  and the grid is internally consistent; corrupt/foreign ledgers (including a
  bad `piece_size`/`pieces` combination or a malformed sidecar) are reset
  rather than fabricating success. A bitmap without an identity is never
  trusted.
- `clean` no longer deletes the bitmap/marker of a transfer whose lock is still
  live; hook/plugin loading errors are passed through the URL redactor. It only
  removes recognized bitmaps, valid orphan completion markers, temp files and
  dead lock directories, so a user data file or directory whose name merely
  ends in those suffixes is never touched. Failed removals are reported and
  make `clean` exit `1` instead of being counted as success.
- A checkpoint whose data `flush`/`fsync` fails is skipped, so the ledger never
  claims bytes that did not reach disk.
- Webhook HTTP 4xx/5xx responses now produce a warning, and webhook payload
  strings (including `on_error`) are redacted recursively.
- `--user` Basic auth uses UTF-8 (matching aiohttp's helper), so non-ASCII
  credentials no longer raise.
- Malformed URLs and schemeless `user:pass@host` proxy strings are sanitized
  instead of printing credentials.
- A source checksum is re-hashed before `--skip-existing` when the proof is a
  complete bitmap or a hole scan rather than a completion marker.
- Resolve failures are recorded in `.downctl.failed` so private-repo failures
  are retryable; `-o` combined with `--checksums` matches the remote filename;
  an enabled `exec` hook no longer injects output into JSON stdout; WSL
  drive-relative/UNC paths are rejected with a clear error.
- Disk pre-flight now budgets the whole object when resume is impossible (no
  bitmap, stale bitmap, truncated/oversized file), instead of counting only
  `size - current` for a file the transfer will reset.
- `--dry-run` no longer rewrites `.downctl.failed` (which discarded unrelated
  pending failures) and no longer creates the destination directory.
  `--dry-run --reporter json` now emits one `dry_run` record per target instead
  of silence.
- `-v` no longer contaminates machine output: `get --reporter json` and
  `probe`/`status`/`clean` with `--json` keep stdout pure even with verbose
  config logging enabled.
- Webhook failure warnings, aiohttp/proxy exception messages and `config show`
  mirror keys are redacted like the rest of the credential surface; a legacy
  failed-log URL is masked by `status`.
- A periodic checkpoint that cannot write its ledger now warns once (the
  previous code ignored `save`'s failure) and the atomic write stays ordered
  with the final snapshot instead of racing a worker thread on cancellation.
- `status`/`clean`/`config`/`plugin`/`doctor` errors (and a closed stdout) are
  reported through the same clean CLI guard as `get`/`probe`, instead of a
  traceback.
- `--user` no longer calls `aiohttp.encode_basic_auth` (missing from several
  supported aiohttp releases); Basic auth is encoded directly.
- Resuming `hf://repo@v1` after switching to `@v2` (or two same-size URLs) can
  no longer splice old pieces into the new file.
- Re-running a completed size-known download on WSL DrvFS no longer re-downloads
  it from scratch: the completion marker is portable proof. Native Windows no
  longer trusts marker-less full-size files (holes are undetectable there).
- `kill -9`/power loss no longer throws away everything since the retry loop
  started (periodic checkpoint).
- Failed S3/private-repo records can be retried: fresh signatures/tokens
  instead of an expired URL. `--retry-failed` matches exact target paths; the
  basename fallback is limited to `-o` renames, so a moved/same-named object is
  not silently substituted. Legacy `file://`/`sftp://` records without an
  origin infer their transport from the stored URL instead of using HTTP.
- Presigned queries and tokens no longer leak through the JSON reporter,
  webhook payloads, `exec` environment, error messages or `config show`.
- `--reporter json` stdout is pure JSON-lines: human summaries are suppressed
  (warnings/errors stay on stderr).
- A successful run no longer leaves an empty `.downctl.failed`.
- `plugin list`/`plugin info` no longer execute local plugin directories unless
  `--local-plugins` is passed.
- Local `OSError`s are reported as I/O errors instead of "network error".
- Run summary throughput counts bytes actually transferred, not the sum of
  requested sizes (resumed/skipped files no longer inflate it).
- `--dry-run` reports `total / done / to fetch` and accounts for resume state.
- An unknown checksum algorithm fails before downloading, with the available
  algorithms listed, instead of re-downloading twice.
- `--force` with `--skip-existing` is rejected instead of silently letting one
  win; invalid numeric options (`--jobs 0`, `--stall 0`, `--retries -1`,
  `--piece-size 1`, `--limit-rate 0`) are rejected with clear messages.
- The rate limiter no longer banks idle credit: a burst cannot exceed the
  requested rate after a retry backoff or idle period.
- tqdm per-task bars allocate distinct rows, so concurrent unknown-size
  downloads no longer overwrite each other.
- `downctl probe` exits `1` for unreachable, non-HTTP(S) or HTTP >= 400
  targets, and says why (text and `--json`); a client timeout is reported as
  unreachable instead of a local I/O error.
- A server that rejects `HEAD` with 405/501 no longer breaks probing: the size
  is read from a ranged `GET` instead of the error page's `Content-Length`.
- Windows paths under WSL (`-d 'D:\models'`, `-o 'D:\x'`) are translated to
  `/mnt/d/...` instead of creating a directory with a backslash in its name.
- Bitmaps that would exceed 2,000,000 pieces (e.g. 100 GiB with 1 KiB pieces)
  are rejected instead of writing a hundreds-of-megabytes ledger.
- `doctor` reads the proxy from the config file as well as the environment.
- Missing `exec`/`checksum` entries were added to the English plugin table, and
  the docs no longer claim that `kill -9` resumes without a checkpoint.

### Changed

- The minimum aiohttp is now **3.11**. downctl uses the session-level
  `proxy` argument (added in 3.11), so the previous `>=3.9` metadata allowed
  installations that crashed when `--proxy` was configured.
- `--skip-existing` means "skip when completion is provable", not "skip when
  the size matches".
- On Windows and WSL DrvFS, a full-size file without a completion marker is
  re-downloaded (use `DOWNCTL_TRUST_PARTIAL=1` to force trust once).
- Failed-log records from 0.1.2 and older are still read; when they lack an
  origin, retry falls back to the stored URL with a warning (presigned URLs are
  likely expired).
- The failed log is written atomically with owner-only permissions where the
  platform supports it (it may contain a URL with a token, which is required
  for retrying).
- `probe` exits `1` when any target fails.
- `downctl plugin` loads local plugin directories only with `--local-plugins`.
- `downctl status`/`clean` print one JSON object with `--json`.

### Removed

- `hf-download.py` (legacy single-file script; not part of the package).
- The unused `aioresponses` development dependency.

### Documentation

- README (zh/en), CLI, configuration, hooks, sources, troubleshooting,
  upgrading and design pages updated for URL identities, completion markers,
  checkpoints, origin-based retry, redaction, JSON purity and the 0.1.3
  behaviour changes.
- Default config template now documents `checkpoint`, `piece_size`,
  `min_split_size`, `[plugins.s3]` and `[plugins.sftp]`.

## [0.1.2] - 2026-10-17

### Added

- resume-aware progress: the engine computes already-complete bytes from the
  piece bitmap and drives optional `run_resumed` / `task_resumed` reporter
  hooks, so bars and JSON no longer understate a resumed download
- bitmaps record their piece size (v3) and are **resliced** when
  `--connections` / `--piece-size` change, instead of discarding progress
- `--force` (re-download from scratch) and `--skip-existing` (skip files that
  already exist with a matching size)
- `--piece-size SIZE` and `--min-split-size SIZE` (plus matching `[core]` keys)
- `-l -` reads targets from stdin (`-l` already accepted a file)
- `downctl probe --json` (one JSON object per target)
- `downctl completions bash|zsh|fish` — dependency-free completion scripts
- `exec` hook (run a shell command on chosen events; `DOWNCTL_*` env vars) and
  `checksum` hook (write `<file>.<algo>` digest sidecars)
- a run summary with elapsed time and average throughput

### Fixed

- `--checksum` with more than one target is rejected (it previously applied the
  same digest to every file); use `--checksums FILE` for batches
- unknown-size streaming downloads now perform a disk-space pre-flight once the
  length is known (`core.disk`)
- GitHub release assets of private repositories are fetched through the API
  asset URL with `Accept: application/octet-stream`, so the token stays on the
  API host instead of leaking to the redirect/CDN host

### Changed

- `-v/--verbose` now emits real diagnostics (resolved tasks, mirror rewrites,
  resume points, effective config)
- test-coverage gate raised to 85%

### Documentation

- new bilingual **Cookbook** page (recipes); README and docs updated for the
  new options, hooks and commands

## [0.1.1] - 2026-10-17

### Fixed

- transfer: an `OSError` raised inside the download task group is unwrapped
  and re-raised, so a disk error fails only that file instead of aborting the
  whole batch
- single-stream fallback now verifies the received length against the expected
  size (`task.size`), not only the response's `Content-Length`, preventing
  silent truncation when a server returns a shorter body
- rate limiting no longer trips the stall watchdog at low limits (a heartbeat
  tracks deliberate throttle waits as activity)
- failed log preserves previous failures for targets not touched by the run
- `clean` no longer removes lock directories owned by a live process
- probe treats any HTTP >= 400 as a failure (4xx permanent, 5xx/other via
  the transport's transient classification)
- an explicit `Authorization` header now overrides configured credentials
  case-insensitively (no duplicate headers)
- source-provided absolute targets are rejected unless explicitly requested
  (`-o/--output`), completing the path-traversal fix
- HTTP transport sends `Accept-Encoding: identity` so length checks are not
  broken by transparent gzip decompression
- SFTP verifies host keys against `known_hosts` when available (warns when
  not; `known_hosts = ""`/`none`/`false` disables); URL credentials are
  percent-decoded; task URLs keep the port
- tar extraction rejects FIFOs/device nodes (also on Python 3.11)
- entry-point plugin loading catches all errors; local modules are registered
  in `sys.modules`
- mirror router matches on a host/path boundary; `webhook` honours an explicit
  empty `events` list; checksum files skip malformed digests
- `-q/-v` are accepted after nested `plugin`/`config` subcommands; the
  `filesystem` sink is implemented
- `init_console`/`safe_io` are now actually invoked (Windows VT colors and
  UTF-8-safe redirected output)
- the HuggingFace token is attached per-request to the HF host only (no
  longer sent to arbitrary download hosts); presigned S3 URLs are redacted in
  logs; `clean` only removes locks whose dead owner can be verified

### Changed

- version bumped to 0.1.1
- `-r/--retries` counts retries (total attempts = retries + 1); the retry
  backoff base is overridable via `DOWNCTL_RETRY_BASE`

### Documentation

- README (zh/en) gains an **Upgrade** section (pip/pipx/source/Docker, verify,
  what is left untouched)
- the docs site is expanded with bilingual Source, Configuration, Hooks,
  Troubleshooting and Upgrading pages (9 pages per language)

## [0.1.0] - 2026-10-17

First release.

### Added

- P1 skeleton: package layout, plugin contracts (7 categories), plugin
  registry/loader/event bus, CLI surface, TOML configuration, packaging and
  CI.
- P2 core engine:
  - resumable piece bitmap (`core.pieces`) with authoritative out-of-order
    resume, `fsync`-backed ledger, atomic save and hole-detection guards
  - multi-connection transfer (`core.transfer`) with stall watchdog, single
    stream fallback for unknown sizes and Range-less servers
  - download orchestration (`core.engine`) with retries, disk pre-flight,
    file locking, checksum verification and a concurrent task runner
  - transport contract now exposes a streaming `Response`; the HTTP transport
    maps aiohttp errors onto the downctl exception hierarchy so the core is
    HTTP-library agnostic
  - integration tests with a controllable local HTTP server (full download,
    out-of-order resume, Range fallback, unknown size, stall-then-resume)
- P4/P5 usable CLI and built-in plugins:
  - `downctl get` resolves targets through source plugins, probes sizes,
    applies mirror rewrites and runs the engine with a chosen reporter
  - `downctl probe`, `plugin list|info`, `config init|show|path`, `doctor`
    and `wizard` are implemented
  - options: `-d/-o/-j/-t/-r/-p`, `--checksum`, `--mirror`, `--source`,
    `--reporter`, `--include/--exclude`, `--dry-run`, `--retry-failed`,
    `-l/--list`, `--local-plugins`
  - failed-target log (`.downctl.failed`) with `--retry-failed`
  - session factory, TOML config wiring and `DOWNCTL_CONFIG` override
  - integration tests drive the real CLI flow against a local server
- HuggingFace source plugin:
  - expands `hf://org/name[@revision]` (and bare `org/name`) for models and
    datasets via the Hub API
  - honours `--include`/`--exclude`, `--revision` and the
    `[plugins.huggingface]` mirror/token options; verifies LFS sha256
  - integration tests against a fake Hub plus real-world verification
- Docs and repository tooling:
  - `py.typed` marker (PEP 561) and inline types
  - docs site config (`mkdocs.yml`) plus CLI and plugin guides
  - issue/PR templates, Dependabot config and `RELEASING.md`
- GitHub Releases source plugin (`gh://owner/repo[@tag]`):
  - lists release assets via the REST API, honours `--include`/`--exclude`
  - uses asset `sha256` digests for verification; `[plugins.github]` api/token
  - integration tests against a fake API plus end-to-end download
- configurable `connections` and `stall` (config and `--connections`/
  `--stall`), threaded through the engine
- S3 source plugin (`s3://bucket/key` and prefixes):
  - SigV4 query-string presigning (no extra dependency), so the HTTP
    transport fetches S3 objects incl. ranged transfers
  - prefix listing via ListObjectsV2 with pagination; `[plugins.s3]`
    endpoint/region/credentials/path_style; object size via presigned HEAD
  - tests against a fake S3 endpoint plus SigV4 unit tests
- resume and failure handling:
  - failed log v2 stores the resolved URL, target path, checksum and size,
    so `--retry-failed` restores repo subpaths and verification
  - `--continue` skips targets that fail to resolve or probe instead of
    aborting the whole run
- unified console output with `-q/--quiet` and `-v/--verbose`; quiet mode
  also switches to the `quiet` reporter; tqdm progress bar polished
- `downctl status` scans a directory for in-progress (piece bitmap) and
  failed downloads
- hook plugins can be enabled with `--hook NAME` or `[core] hooks = [...]`
- `--user-agent` for a custom HTTP User-Agent
- Dockerfile (multi-stage, wheel-based) and `.dockerignore`
- aggregate progress: the tqdm reporter shows an overall bar across files
- `archive` hook extracts zip/tar archives on completion (path-traversal and
  link-safe); `keep`/`into` options
- `--checksums FILE` verifies downloads against a `sha256sum`-style file
- SFTP source and transport (`sftp://[user@]host[:port]/path`), optional
  `asyncssh` dependency (`downctl[sftp]`); supports directory listing and
  ranged (multi-connection) transfers
- `webhook` hook POSTs selected lifecycle events as JSON
- `manifest` hook writes a `sha256sum` manifest of completed downloads
- coverage gate (75%) in the test configuration and CI
- documentation: the primary `README.md` is now a detailed Chinese guide
  (install, usage, sources, configuration, plugins, troubleshooting); the
  English version lives at `README.en.md`
- global rate limiting: the `--limit-rate` budget is now shared across all
  concurrent files (previously each file was limited independently)
- `-H/--header` (repeatable) and `-u/--user USER:PASS` for custom headers and
  HTTP Basic auth
- `downctl clean [DIR]` removes leftover bitmaps, the failed log and stale
  lock directories
- `file://` source and transport for local files/directories (recursive,
  ranged); console warnings/errors are now colored on a TTY
- concurrent downloads (job > 1) show an overall progress bar with a
  completed-files count; optional shell completion via `argcomplete`
  (`downctl[completion]`)
- docs site published to GitHub Pages (Actions-based, keeping a single
  master branch) with a generated coverage badge; CI gains a `pip-audit`
  dependency-vulnerability job

### Fixed

- webhook hook now flushes in-flight POSTs at shutdown (via an event-bus
  `aclose`), so late events such as `on_task_finish`/`on_finish` are no
  longer dropped
- the CLI reports network/library errors as clean messages instead of
  dumping a traceback (use `-v` for the traceback)
- single-stream downloads no longer cancel the read generator to poll for
  progress (which could look like a clean EOF and truncate the file); a
  watchdog task aborts instead
- reject piece bitmaps whose length does not match the piece count
- confine source-provided relative targets to the destination directory
  (mitigates `..`/percent-encoded path traversal from remote metadata)
- lock acquisition treats a missing pid file as held (avoids a takeover race)
- a file that cannot be opened for writing fails instead of retrying forever
- an unknown checksum algorithm now fails the file instead of silently passing
- checksum give-up discards the bad file so it is not trusted on the next run
- SFTP: guard connection creation with a lock, key the cache by credentials,
  evict broken connections, and never leak inline credentials across hosts
- archive hook normalizes backslashes (Windows traversal) and extracts off the
  event loop; `logfile`/SFTP key paths expand `~`; manifest uses POSIX
  separators; GitHub tag is URL-quoted and private assets carry the token;
  JSON reporter throttles progress; HTTP 408/425 are retried

### Changed

- version is now dynamic, sourced from `src/downctl/__init__.py`
- the docs site is bilingual (English + 简体中文) via `mkdocs-static-i18n`

[0.1.4]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.4
[0.1.3]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.3
[0.1.2]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.2
[0.1.1]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.1
[0.1.0]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.0
