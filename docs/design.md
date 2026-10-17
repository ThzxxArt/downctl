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
| Verifier | Check digests | `sha256` |
| Sink | Persist bytes | `filesystem` |
| Reporter | Progress & logs | `tqdm`, `json`, `quiet` |
| Router | Rewrite URLs | `mirror` |
| Hook | Lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum` |

> The **Sink** contract is defined and the `filesystem` sink is implemented;
> the core currently performs ranged writes directly to the target file, so
> sinks are available to plugins but not on the engine's hot path.

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
│   ├── cli.py, app.py, config.py, session.py, probe.py, console.py, platform_utils.py
│   ├── core/            # stable engine (not plugins)
│   │   ├── engine.py transfer.py pieces.py watchdog.py
│   │   ├── ratelimit.py locks.py task.py context.py errors.py
│   ├── plugin/          # registry + loader + event bus + protocols
│   └── builtin/         # built-in plugins (via the plugin interface)
│       ├── sources/{url,huggingface,github,s3,sftp,file}, _sigv4
│       ├── transports/{http,sftp,file}, _sftp
│       ├── verifiers/, sinks/, reporters/, routers/, hooks/
├── tests/{unit, integration}/
├── docs/  (bilingual site)
├── examples/plugins/
└── .github/workflows/{ci, docs, release, codeql}.yml
```

---

## 4. Plugin system

### 4.1 Discovery (highest priority first)

1. Local directories: `./downctl_plugins/`, `~/.config/downctl/plugins/`
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
- On interruption the data file is `fsync`-ed, then the bitmap is written
  atomically; a retry re-fetches only missing pieces.
- The bitmap records its piece size (v3), so changing `--connections` /
  `--piece-size` **reslices** completion state by byte range instead of
  discarding progress.
- Reporters can seed a resume-aware bar: the engine computes already-complete
  bytes from the bitmap and calls the optional `run_resumed` / `task_resumed`
  hooks.
- Without a bitmap, a full-size file is trusted only if it provably has no
  holes (SEEK_HOLE), the NTFS sparse flag is clear, or the user opts in with
  `DOWNCTL_TRUST_PARTIAL=1`.

---

## 6. CLI

```
downctl get <target>…      download (source auto-selected by scheme)
downctl probe <target>…    size/status only
downctl status [DIR]       incomplete/failed downloads
downctl clean [DIR]        remove leftover bitmaps / failed log / locks
downctl plugin list|info   plugin management
downctl config init|show|path
downctl doctor             environment diagnostic
downctl wizard             interactive
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
- **CI**: lint (ruff/mypy) + matrix tests (ubuntu/macos/windows × 3.11–3.13,
  coverage ≥ 75%) + build/twine-check + `pip-audit`.
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

Optional next: richer reporter (rich), Homebrew / standalone binaries,
additional sources, sink backends wired onto the hot path.
