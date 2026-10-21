# Hooks

Hooks observe the download lifecycle. They are **opt-in**: enable per run with
`--hook NAME` (repeatable) or for every run via `[core] hooks = [...]`. Their
options live under `[plugins.<name>]`.

## Events

| Event | Payload |
|---|---|
| `on_start` | `{targets: [...]}` (plus `job` inside a batch) |
| `on_task_start` | `{task}` |
| `on_task_finish` | `{task, ok, witness}`; a skip adds `skipped: true` |
| `on_error` | `{task, error}` |
| `on_finish` | `{succeeded, failed, hooks_failed, bytes, elapsed}` (plus `job` inside a batch) |

`hooks_failed` is the number of hook failures seen during the run. It is counted
after in-flight hook work has been drained, so it is final rather than a snapshot
taken before the checksum/exec/webhook threads reported.

Built-in hooks: `logfile`, `archive`, `webhook`, `manifest`, `exec`, `checksum`,
`metrics`, `notify`.

`witness` is `validator` (the object's version witness matched), `checksum` (a
supplied digest was verified) or `none` (nothing about the object version could
be checked, so the skip is reported as unversioned). It is emitted for a task
that finished successfully; a failure is reported through `on_error`.
`checksum` and `manifest` produce **no artifact** when `witness = none`: both are
fed back into `--checksums`, so writing one for a file that was never tied to a
version would be a promise nobody kept.

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
traversal; tar entries that are links, devices or FIFOs are refused, and a
declared expansion beyond the limits is refused before extraction. Zip entries
are only path-checked: Python's `zipfile` materializes every member as a regular
file, so the tar-style link/device rejection does not apply to them. A skipped
task (`skipped: true`) is not extracted again: the run that produced it already
did.

| Key | Default | Meaning |
|---|---|---|
| `into` | *(sibling dir named after the archive)* | Destination directory |
| `keep` | `true` | Keep the archive after extraction |
| `max_entries` | `200000` | Refuse an archive declaring more entries (`0` = no limit) |
| `max_total_bytes` | `20 GiB` | Refuse an archive declaring more uncompressed bytes (`0` = no limit) |

A declared expansion ratio over `1000x` the compressed size is refused as well
(`.zip` and `.tar.*` both).

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
`--checksums`. New entries are **merged** into an existing manifest instead of
overwriting it, so a second run over one file in a directory does not erase
what the first run proved.

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
| `events` | `["on_task_finish"]` | Events that trigger writing. Only `on_task_finish` is accepted: it is the only event that carries the witness the sidecar claims to rest on |

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

---

## `metrics`

Write the run's totals as a Prometheus textfile on `on_finish`, atomically
(`<path>.<pid>.tmp` + `os.replace`), so a scrape sees either the previous
complete file or the new one.

| Key | Default | Meaning |
|---|---|---|
| `path` | `downctl.prom` | Output file (`~` is expanded) |
| `labels` | `[]` | Extra `name=value` labels added to every metric |

```toml
[core]
hooks = ["metrics"]

[plugins.metrics]
path = "/var/lib/node_exporter/textfile/downctl.prom"
labels = ["host=build-01"]
```

Series: `downctl_run_duration_seconds`, `downctl_files_total{outcome}`,
`downctl_bytes_total`, `downctl_failures_total{reason}` and
`downctl_hook_failures_total`. A failed write is a hook failure, visible to
`strict_hooks`.

---

## `notify`

Send a one-line run summary over HTTP or by running a command.

| Key | Default | Meaning |
|---|---|---|
| `url` | *(none)* | POST endpoint. Without `topic`, a generic webhook; with `topic`, the ntfy server base (e.g. `https://ntfy.sh`) |
| `command` | *(none)* | Run a shell command instead; the summary is passed in `DOWNCTL_*` variables |
| `topic` | *(none)* | ntfy topic. Switches the POST body to ntfy's JSON |
| `timeout` | `10.0` | Timeout for `command`, in seconds (must be positive); a timed-out command is killed as a process group |
| `events` | `["on_finish"]` | Events that trigger a notification |
| `only_failures` | `false` | Skip a clean `on_finish` |
| `title` | `downctl` | Notification title |

`url` and `command` are mutually exclusive; setting both is a configuration
error. A plain `url` receives downctl's generic webhook JSON
(`{"title", "message", "url"}`). Add `topic` to POST to an ntfy server instead;
the body then uses ntfy's schema (`{"topic", "message", "title"}`), with the
message in `message` rather than `body`.

```toml
[core]
hooks = ["notify"]

[plugins.notify]
url = "https://ntfy.sh"     # server base, not the topic URL
topic = "my-downctl"
timeout = 10                # command timeout; float accepted
only_failures = true
```

For a generic webhook receiver, omit `topic` and point `url` at the endpoint:

```toml
[plugins.notify]
url = "https://example.com/downctl-notify"
title = "downctl nightly"
```

Delivery happens in the background (or a subprocess), so a slow notifier never
blocks a download. The command receives `DOWNCTL_EVENT`, `DOWNCTL_SUCCEEDED`,
`DOWNCTL_FAILED`, `DOWNCTL_BYTES` and `DOWNCTL_ELAPSED`. The body is redacted;
failures are reported and counted.
