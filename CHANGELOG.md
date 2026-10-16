# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/ThzxxArt/downctl/releases/tag/v0.1.0
