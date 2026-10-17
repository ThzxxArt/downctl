# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.2]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.2
[0.1.1]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.1
[0.1.0]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.0
