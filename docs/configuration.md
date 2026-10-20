# Configuration

`downctl` reads a TOML file at `~/.config/downctl/config.toml` (override with
the `DOWNCTL_CONFIG` env var). Command-line options override config values.
Values support `${ENV_VAR}` interpolation. Unknown keys are ignored.

```bash
downctl config path     # where the config lives
downctl config init     # write a default config
downctl config show     # print the effective config
```

## Precedence

1. Command-line options (highest)
2. Config file values
3. Built-in defaults (lowest)

Environment variables are used where a config key is unset (e.g. `HF_TOKEN`,
`GITHUB_TOKEN`, AWS credentials, `https_proxy`).

## `[core]`

| Key | Default | Meaning |
|---|---|---|
| `dir` | `~/Downloads` | Destination directory |
| `jobs` | `1` | Concurrent files |
| `retries` | `10` | Retries per file (`0` = retry forever; probing is always bounded) |
| `connections` | `16` | Connections per file |
| `stall` | `30` | Stall-watchdog seconds |
| `checkpoint` | `30` | Seconds between durable resume snapshots (`0` = only on exit) |
| `max_time` | `0` | Wall-clock cap in seconds for a single target (`0` = no limit) |
| `probe_jobs` | `8` | Targets resolved/probed concurrently (`0` = follow `jobs`) |
| `require_witness` | `false` | Refuse to skip or resume state whose object version cannot be checked |
| `piece_size` | `0` | Explicit piece size in bytes (`0` = auto) |
| `min_split_size` | `0` | Files smaller than this use one connection (`0` = always split) |
| `rate` | *(none)* | Byte/second limit (shared across files); `<= 0` = unlimited |
| `proxy` | *(none)* | Proxy URL |
| `mirror` | `{}` | `FROM = "TO"` URL prefix rewrites |
| `hooks` | `[]` | Hook plugins enabled every run |
| `strict_hooks` | `false` | Treat a failing hook (extraction that raised, command that exited non-zero) as a failed run. Hook failures are always reported and counted; this only decides the exit code |

### Concurrency semantics

Three numbers interact, and they mean different things:

| Option | Bounds | Applies to |
|---|---|---|
| `jobs` | How many *files* are open at once | The download phase |
| `connections` | How many sockets one file may use | A single file, once it is split into pieces |
| `probe_jobs` | How many targets are resolved/probed at once | The phase *before* downloading (`0` = follow `jobs`) |

`connections` is a per-file ceiling, not a global one: `jobs=4` with
`connections=16` may hold up to 64 sockets. `rate`, by contrast, is one budget
shared by the whole process. A file smaller than `min_split_size` uses a single
connection regardless of `connections`.

## `[plugins.<name>]`

Each plugin reads its own table.

### `[plugins.huggingface]`

| Key | Meaning |
|---|---|
| `token` | Access token (or `HF_TOKEN`) |
| `mirror` | Host override, e.g. `https://hf-mirror.com` |
| `token_to_mirror` | Send the token to `mirror` as well (default `false`) |

A token belongs to the host that issued it. Configuring a `mirror` is not consent
to hand that host the credential, so by default the token goes to
`huggingface.co` only and a mirror that needs authentication must be opted in
with `token_to_mirror = true` (a 401 caused by the withheld token says so).

### `[plugins.github]`

| Key | Meaning |
|---|---|
| `token` | API token (or `GITHUB_TOKEN` / `GH_TOKEN`) |
| `api` | API base URL (GitHub Enterprise) |

### `[plugins.s3]`

| Key | Default | Meaning |
|---|---|---|
| `region` | `us-east-1` | Signing region |
| `access_key` / `secret_key` / `session_token` | env | Credentials |
| `endpoint` | `https://s3.<region>.amazonaws.com` | Endpoint. A path prefix (`https://host/s3`) is signed and requested as part of the object URI |
| `path_style` | `true` | Path-style addressing |
| `expires` | `3600` | Presigned URL lifetime (1–604800 s) |

### `[plugins.sftp]`

| Key | Meaning |
|---|---|
| `user` / `password` | Credentials |
| `key` / `key_passphrase` | Client key |
| `port` | Port (default 22) |
| `known_hosts` | Host-key file; `""`/`none`/`false` disables verification |

### `[plugins.tqdm]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Show progress bars |

### Hooks (`[plugins.logfile]`, `[plugins.archive]`, `[plugins.webhook]`, `[plugins.manifest]`, `[plugins.exec]`, `[plugins.checksum]`)

See [Hooks](hooks.md).

## Full example

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 3
connections = 16
stall = 30
checkpoint = 30
# piece_size = 8388608
# min_split_size = 1048576
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["archive", "manifest"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"

[plugins.s3]
# region = "us-east-1"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# session_token = "${AWS_SESSION_TOKEN}"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# path_style = true
# expires = 3600

[plugins.sftp]
# user = "me"
# password = "${SFTP_PASSWORD}"
# key = "~/.ssh/id_ed25519"
# port = 22
# known_hosts = "~/.ssh/known_hosts"

[plugins.archive]
# into = "~/Downloads/extracted"
# keep = true

[plugins.webhook]
# url = "https://example.com/downctl"
# events = ["on_finish"]

[plugins.manifest]
# filename = ".downctl.manifest"

[plugins.tqdm]
enabled = true
```

## Environment variables

| Variable | Purpose |
|---|---|
| `DOWNCTL_CONFIG` | Config file path |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | HuggingFace token |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub API token |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | S3 credentials |
| `https_proxy` / `HTTP_PROXY` / … | Proxy |
| `WSL_HOST_IP` | WSL host IP for proxy auto-detection (a loopback value disables the guess) |
| `NO_COLOR` | Disable colored output |
| `DOWNCTL_TRUST_PARTIAL` | Trust a full-size local file whose holes can't be probed |
| `DOWNCTL_RETRY_BASE` | Retry backoff base in seconds (default 5) |

## Validation

Numeric keys are validated on load: `jobs`, `connections`, `stall` must be
`>= 1`; `retries >= 0`; `checkpoint >= 0` (`0` disables periodic snapshots);
`max_time >= 0` and `probe_jobs >= 0` (`0` = no limit / follow `jobs`);
`rate <= 0` means unlimited. Invalid values abort with a clear message.
Command-line overrides are validated too (`--jobs 0`, `--stall 0`,
`--retries -1`, `--max-time -1`, `--probe-jobs -1`, `--piece-size 1`,
`--limit-rate 0` are rejected).

`downctl config show` masks secret-looking plugin keys (`token`, `password`,
`secret_key`, …) and proxy userinfo; the file on disk is unchanged.

`DOWNCTL_TRUST_PARTIAL=1` only affects the full-size, marker-less file heuristic
on filesystems where holes cannot be proven. downctl's own downloads carry a
`.downctl-done` marker and do not need it.
