# Hooks

Hooks observe the download lifecycle. They are **opt-in**: enable per run with
`--hook NAME` (repeatable) or for every run via `[core] hooks = [...]`. Their
options live under `[plugins.<name>]`.

## Events

| Event | Payload |
|---|---|
| `on_start` | `{targets: [...]}` |
| `on_task_start` | `{task}` |
| `on_task_finish` | `{task, ok}` |
| `on_error` | `{task, error}` |
| `on_finish` | `{succeeded, failed}` |

Built-in hooks: `logfile`, `archive`, `webhook`, `manifest`.

---

## `logfile`

Append one TSV line per event to a file.

| Key | Default | Meaning |
|---|---|---|
| `path` | `downctl.log` | Log file (`~` is expanded) |

```toml
[core]
hooks = ["logfile"]

[plugins.logfile]
path = "~/.cache/downctl/run.log"
```

---

## `archive`

Extract a downloaded archive when its task finishes. Hardened against path
traversal and refuses links/devices/FIFOs.

| Key | Default | Meaning |
|---|---|---|
| `into` | *(sibling dir named after the archive)* | Destination directory |
| `keep` | `true` | Keep the archive after extraction |

Supported: `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`,
`.tar.xz`/`.txz`.

```toml
[core]
hooks = ["archive"]

[plugins.archive]
into = "~/Downloads/extracted"
keep = false
```

---

## `webhook`

POST selected events as JSON (fire-and-forget; flushed before exit).

| Key | Default | Meaning |
|---|---|---|
| `url` | *(none)* | Endpoint (required) |
| `events` | `["on_finish"]` | Events to send |
| `timeout` | `30` | Request timeout (s) |

```toml
[core]
hooks = ["webhook"]

[plugins.webhook]
url = "https://example.com/downctl"
events = ["on_start", "on_task_finish", "on_finish"]
```

The task object is serialized as `{url, target, source}`.

---

## `manifest`

Write a `sha256sum`-style manifest of completed downloads, usable later with
`--checksums`.

| Key | Default | Meaning |
|---|---|---|
| `filename` | `.downctl.manifest` | Manifest file name |

```toml
[core]
hooks = ["manifest"]

[plugins.manifest]
filename = ".downctl.manifest"
```

Verify a re-download against a previous run:

```bash
downctl get <url> -d new --checksums old/.downctl.manifest
```
