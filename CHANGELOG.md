# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ThzxxArt/downctl/commits/main
