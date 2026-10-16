# Plugin development

downctl is built around one idea: **everything is a plugin**. The stateful core
(scheduling, piece bitmaps, the stall watchdog, rate limiting, locking, disk
checks) is stable; every strategy — sources, transports, verifiers, sinks,
reporters, routers and hooks — is a plugin, and the built-ins are no different.

## Categories

| Category | entry-point group | Job | Built-ins |
|---|---|---|---|
| Source | `downctl.sources` | resolve input into tasks | `url`, `huggingface` |
| Transport | `downctl.transports` | transfer bytes | `http` |
| Verifier | `downctl.verifiers` | check digests | `sha256` |
| Sink | `downctl.sinks` | persist bytes | `filesystem` |
| Reporter | `downctl.reporters` | progress & logs | `tqdm`, `json`, `quiet` |
| Router | `downctl.routers` | rewrite URLs | `mirror` |
| Hook | `downctl.hooks` | lifecycle events | `logfile` |

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

- [`docs/DESIGN.md`](DESIGN.md) — architecture and the core/plugin boundary
- [`examples/plugins/`](https://github.com/ThzxxArt/downctl/tree/main/examples/plugins)
  — a minimal installable plugin
