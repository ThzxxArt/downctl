# Hooks

Hooks observe the download lifecycle. They are **opt-in**: enable per run with
`--hook NAME` (repeatable) or for every run via `[core] hooks = [...]`. Their
options live under `[plugins.<name>]`.

## Events

| Event | Payload |
|---|---|
| `on_start` | `{targets: [...]}` |
| `on_task_start` | `{task}` |
| `on_task_finish` | `{task, ok, witness}`; a skip adds `skipped: true` |
| `on_error` | `{task, error}` |
| `on_finish` | `{succeeded, failed}` |

Built-in hooks: `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum`.

`witness` is `validator` (the object's version witness matched), `checksum` (a
supplied digest was verified) or `none` (nothing about the object version could
be checked, so the skip is reported as unversioned). It is emitted for a task
that finished successfully; a failure is reported through `on_error`.

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

The task object is serialized as `{url, target, source, origin}`. `url` and
`origin` are credential-masked (a presigned query is stripped), so a webhook
endpoint never receives an S3 signature or a token in the URL.

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

---

## `exec`

Run a shell command when a lifecycle event fires.

| Key | Default | Meaning |
|---|---|---|
| `command` | *(none)* | Shell command (required) |
| `events` | `["on_task_finish"]` | Events that trigger it |
| `timeout` | `300` | Seconds before the command is killed |

The event details are exposed as environment variables: `DOWNCTL_EVENT`,
`DOWNCTL_TARGET`, `DOWNCTL_PATH`, `DOWNCTL_URL`, `DOWNCTL_ORIGIN`,
`DOWNCTL_OK`. `DOWNCTL_URL` strips a presigned query; both URLs remove
userinfo and mask secret-looking query values, so a hook script cannot
accidentally log a signature. In JSON-reporter mode the command's stdout is
redirected to stderr so hook output cannot corrupt the JSON stream.

```toml
[core]
hooks = ["exec"]

[plugins.exec]
command = "echo \"$DOWNCTL_TARGET -> $DOWNCTL_PATH\" >> ~/downloads.log"
```

> **Security:** the command runs with your privileges. Only configure commands
> you trust.

---

## `checksum`

Write a `<file>.<algo>` digest sidecar for each completed download, in
`sha256sum` line format (`<hex>  <basename>`) — consumable later by
`--checksums`.

| Key | Default | Meaning |
|---|---|---|
| `algo` | `sha256` | Hash algorithm (`sha256`/`sha1`/`sha512`/`md5`) |
| `events` | `["on_task_finish"]` | Events that trigger writing |

Every supported algorithm has a built-in verifier, so a sidecar written with any
of them can be fed back through `--checksums FILE`. Prefer `sha256` (or
`sha512`) when the publisher offers a choice; `md5`/`sha1` exist for
compatibility with upstream checksum files.

```toml
[core]
hooks = ["checksum"]

[plugins.checksum]
algo = "sha256"
```
