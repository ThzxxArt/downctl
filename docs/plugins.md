# Plugin development

downctl is built around one idea: **everything is a plugin**. The stateful core
(scheduling, piece bitmaps, the stall watchdog, rate limiting, locking, disk
checks) is stable; every strategy — sources, transports, verifiers, reporters,
routers and hooks — is a plugin, and the built-ins are no different.

## Categories

| Category | entry-point group | Job | Built-ins |
|---|---|---|---|
| Source | `downctl.sources` | resolve input into tasks | `url`, `huggingface`, `github`, `s3`, `sftp`, `file` |
| Transport | `downctl.transports` | transfer bytes | `http`, `sftp`, `file` |
| Verifier | `downctl.verifiers` | check digests | `sha256`, `sha1`, `sha512`, `md5` |
| Reporter | `downctl.reporters` | progress & logs | `tqdm`, `json`, `quiet` |
| Router | `downctl.routers` | rewrite URLs | `mirror` |
| Hook | `downctl.hooks` | lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum` |

> The `sink` category was removed in 0.1.4: it was never called on the write path
> (the core always wrote through `core/transfer.py`), so it promised a
> replaceable destination that never took effect. An installed package that
> still declares `downctl.sinks` gets a warning from the loader, and
> `downctl plugin list` no longer shows a `sink` line — `API_VERSION` stays
> `"1"`, so plugins in the other six categories keep working.

## Contracts

A plugin is any object exposing the metadata (`name`, `api_version`,
`priority`) plus the methods of one of the protocols in
`downctl.plugin.protocols`. You do **not** need to import downctl to be
discovered — the protocols are structural — but using them for type hints is
encouraged.

```python
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION


class ExampleSource:
    name = "example"
    api_version = API_VERSION  # "1"
    priority = 0

    def can_handle(self, target: str) -> bool:
        return target.startswith("example://")

    async def resolve(self, target: str, ctx) -> list[DownloadTask]:
        name = target[len("example://") :]
        return [DownloadTask(url=f"https://example.com/{name}", target=name, source=self.name)]
```

`ctx` is a `downctl.core.context.RuntimeContext` giving access to the shared
`aiohttp` session, the effective config, the registry, the event bus and
per-run `params`.

## Discovery

Plugins are discovered from three places, in ascending priority (a later
source can shadow an earlier one by name):

1. **Built-ins** (`downctl.builtin`)
2. **Installed packages** advertising a `downctl.*` entry-point
3. **Local directories** — `./downctl_plugins` and
   `~/.config/downctl/plugins` (opt-in via `--local-plugins`)

### Packaging a plugin

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

Install it (`pip install .`) and downctl picks it up automatically.

### Enabling plugins

Sources, reporters and routers are selected automatically or via CLI flags
(`--source`, `--reporter`, `--mirror`). Hook plugins are opt-in: enable them
per run with `--hook NAME` or for every run via the config file. Hook options
come from their `[plugins.<name>]` table.

```toml
[core]
hooks = ["logfile"]

[plugins.logfile]
path = "~/.cache/downctl/run.log"
```

The `archive` hook extracts zip/tar archives when their download finishes:

```toml
[core]
hooks = ["archive"]

[plugins.archive]
into = "~/Downloads/extracted"   # optional; default: sibling dir
keep = false                     # delete the archive after extraction
```

The `webhook` hook POSTs selected events as JSON, and `manifest` writes a
`sha256sum` manifest of completed downloads (usable later with `--checksums`):

```toml
[core]
hooks = ["webhook", "manifest"]

[plugins.webhook]
url = "https://example.com/downctl"
events = ["on_finish"]

[plugins.manifest]
filename = ".downctl.manifest"
```

### Local plugins

Place a module in `./downctl_plugins/` that defines a `register(registry)`
function:

```python
def register(registry):
    registry.register("source", ExampleSource(), origin="example")
```

> **Security:** plugins execute arbitrary code. Only install packages or
> enable directories you trust. `--local-plugins` is opt-in for this reason.

## API versioning

The plugin contract is versioned by `downctl.API_VERSION` (currently `"1"`).
A plugin declares the version it was built against; the registry rejects
mismatches. Breaking changes to a contract bump the API version.

## See also

- [`design.md`](design.md) — architecture and the core/plugin boundary
- [`examples/plugins/`](https://github.com/ThzxxArt/downctl/tree/master/examples/plugins)
  — a minimal installable plugin
