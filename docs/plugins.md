# Plugin development

downctl is built around one idea: **everything is a plugin**. The stateful core
(scheduling, piece bitmaps, the stall watchdog, rate limiting, locking, disk
checks) is stable; every strategy — sources, transports, verifiers, reporters,
routers, hooks, job sources, auth providers and servers — is a plugin, and the
built-ins are no different.

## Categories

| Category | entry-point group | Job | Built-ins |
|---|---|---|---|
| Source | `downctl.sources` | resolve input into tasks | `url`, `http_index`, `huggingface`, `github`, `s3`, `sftp`, `file` |
| Transport | `downctl.transports` | transfer bytes | `http`, `sftp`, `file` |
| Verifier | `downctl.verifiers` | check digests | `sha256`, `sha1`, `sha512`, `md5` |
| Reporter | `downctl.reporters` | progress & logs | `tqdm`, `json`, `quiet` |
| Router | `downctl.routers` | rewrite URLs | `mirror` |
| Hook | `downctl.hooks` | lifecycle events | `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum`, `metrics`, `notify` |
| Job source | `downctl.jobsources` | read a batch file | `toml`, `json` |
| Auth provider | `downctl.auths` | produce credentials for a host | `env`, `command`, `netrc` |
| Server | `downctl.servers` | hand files to another machine | `http` |

> The `sink` category was removed in 0.1.4: it was never called on the write path
> (the core always wrote through `core/transfer.py`), so it promised a
> replaceable destination that never took effect. An installed package that
> still declares `downctl.sinks` gets a warning from the loader. `API_VERSION`
> stays `"1"`, so plugins in the other categories keep working.

Built-in plugins are registered **by code only**. They no longer appear in
`metadata.entry_points`, so an installed `downctl` does not advertise itself as
a plugin package. The nine groups above are the only ones scanned; a group that
merely starts with `downctl.` is not a plugin discovery channel.

## Contracts

A plugin is any object exposing the metadata (`name`, `api_version`,
`priority`) plus the methods of one of the protocols in
`downctl.plugin.protocols`. You do **not** need to import downctl to be
discovered — the protocols are structural — but using them for type hints is
encouraged.

`priority` orders sources that accept the same target (higher wins; ties are
broken by plugin name). It must be an `int`: `"high"`, `1.5` and `True` are
rejected at registration with a `PluginError` that names the plugin and the
value. Registering first and crashing later during source selection is the
failure mode this prevents.

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
source can shadow an earlier one by name, with a warning):

1. **Built-ins** (`downctl.builtin`)
2. **Installed packages** advertising one of the nine `downctl.*` groups
3. **Local directories** — `./downctl_plugins` and
   `$XDG_CONFIG_HOME/downctl/plugins` (`~/.config/downctl/plugins` when unset;
   opt-in via `--local-plugins`)

An entry point may load a class, an instance, or a zero-argument factory
function. The plugin's own `name` attribute is the registry key; the
entry-point name only labels it. Registering the same name twice replaces the
previous implementation with a warning, and `downctl plugin info NAME` shows
`origin`, `api_version`, `options` and (when present) `capabilities` so you can
see which one won.

Optional capability flags are promises only when they are `True`.
`capabilities()` (and therefore `plugin info`) reports `machine_readable`,
`sends_content_range` and `supports_filters` only for an explicit `True`;
`aclose` is reported when it is callable. A source that sets
`supports_filters = False` has no listing to filter, and a reporter that sets
`machine_readable = False` is human-readable — neither is reported as
supporting the capability. The source flag defaults to `True` (the plain-URL
source opts out); the other two default to `False`.

Built-in *sources* claim their schemes at `priority = 10` (`url` is `0`,
`http_index` is `5`). A third-party source therefore needs a **higher** priority
to win for the same target; `--source NAME` still forces one explicitly.

### Packaging a plugin

```toml
[project.entry-points."downctl.sources"]
example = "my_package.source:ExampleSource"
```

The value may also be a factory:

```toml
[project.entry-points."downctl.jobsources"]
myjobs = "my_package.jobs:make_source"
```

Install it (`pip install .`) and downctl picks it up automatically. To start
from a working skeleton instead of an empty file:

```bash
downctl plugin new example --kind source --dir .
pip install -e example_plugin
downctl plugin check example_plugin.plugin
```

`downctl plugin check` also accepts a module (`pkg.module`, `pkg.module:Attr`)
or an installed distribution name, and exits `1` on any contract problem. The
same checks are importable from `downctl.plugin.testing` (`check_plugin`,
`capabilities`, `option_names`, `assert_registers`, `fake_context`).

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

The `notify` hook sends a one-line run summary to a generic webhook, or to an
[ntfy](https://ntfy.sh/) topic when `topic` is set:

```toml
[core]
hooks = ["notify"]

[plugins.notify]
# Generic webhook: receives {"title", "message", "url"}.
url = "https://example.com/hooks/downctl"
title = "nightly models"
```

```toml
[plugins.notify]
# ntfy: POST {"topic", "message", "title"} to the base URL.
url = "https://ntfy.sh"
topic = "my-downctl"
only_failures = true
```

Set exactly one of `url` and `command`; a command that runs longer than
`timeout` (default `10.0` seconds) is killed as a process group and reported as
a hook failure.

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

`checksum` and `manifest` are *evidence* hooks: when `on_task_finish` reports
`witness = none` (a completion nothing could tie to an object version), they
write **no sidecar and no manifest entry**. Both artifacts are fed back into
`--checksums`, so writing one for an unproven file would be a promise nobody
kept.

The newer categories are selected differently: a `jobsource` is chosen by the
batch file's extension (`--job-source` overrides), an `auth` provider is named
by `[auth."<host>"] provider = "..."`, and the `server` used by `downctl serve`
is looked up by name (`http` today). Their protocol methods are small:

```python
from downctl.core.credentials import AuthMaterial


class ExampleAuth:
    name = "example"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, host: str) -> bool:
        return host.endswith("example.com")

    async def material(self, host, ctx, options):
        return AuthMaterial(headers={"Authorization": "Bearer ..."}, source="example")
```

The resolver calls `can_handle(host)` before `material`. Returning `False`
declines the host: `material` is not called and no material is resolved for
that rule (it is an answer, not a failed provider, so it is not an error).
Raising is a failure: it is reported as an `AuthError` and is not cached, so
the next request retries. Both methods receive the lower-cased,
IDNA-normalized hostname with no port or userinfo: `can_handle("example.com")`
is what `https://user:pass@example.com:8443/x` asks about, and a rule written
as `bücher.example` matches `xn--bcher-kva.example` too. Built-in providers
return `True`: naming one in a rule already decides the host.

`AuthResolver.material_for` also accepts a bare host (`huggingface.co`) as
well as a full URL, because the rule is about the host; ports and userinfo are
ignored for matching.

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
