# CLI reference

```
downctl [--version] <command> [options]
```

## `downctl get`

Download one or more targets. An HTTP(S) URL is handled by the `url` source;
`hf://org/name[@revision]` (or a bare `org/name`) by the `huggingface` source;
`gh://owner/repo[@tag]` by the `github` source; `s3://bucket/key` (or
`s3://bucket/prefix/`) by the `s3` source; `sftp://[user@]host[:port]/path`
by the `sftp` source (requires `downctl[sftp]`); `file:///path` by the `file`
source.

| Option | Description |
|---|---|
| `-d, --dir DIR` | Destination directory |
| `-o, --output PATH` | Save a single download to PATH |
| `-j, --jobs N` | Concurrent files |
| `--connections N` | Per-file connections (default 16) |
| `--stall N` | Stall watchdog seconds (default 30) |
| `-t, --limit-rate RATE` | Rate limit (`5M`, `1m`, `512k`) |
| `-r, --retries N` | Max retries per file (`0` = retry forever) |
| `--checksum ALGO:HEX` | Verify after download (e.g. `sha256:...`) |
| `--checksums FILE` | Verify against a `sha256sum`-style file |
| `-p, --proxy URL` | Proxy URL |
| `-H, --header K:V` | Extra request header (repeatable) |
| `-u, --user USER:PASS` | HTTP Basic auth |
| `--mirror FROM=TO` | Rewrite a URL prefix before fetching |
| `--source NAME` | Force a source plugin |
| `--reporter NAME` | Progress reporter (`tqdm`, `json`, `quiet`) |
| `--include PAT` | Include globs (repo sources), repeatable |
| `--exclude PAT` | Exclude globs (repo sources), repeatable |
| `--revision REV` | Repo revision |
| `-l, --list FILE` | Read targets from a file (one per line, `#` comments) |
| `--dry-run` | Probe only, do not download |
| `--retry-failed` | Retry targets from the last failed log |
| `--continue` | Keep going when a target cannot be resolved or probed |
| `--hook NAME` | Enable a hook plugin for this run (repeatable) |
| `--user-agent UA` | Custom HTTP User-Agent |
| `--local-plugins` | Also load plugins from local plugin directories |

Downloaded files resume automatically: interrupt and re-run the same command.
Failures are recorded in `.downctl.failed` next to the output; `--retry-failed`
re-downloads them to the same paths (including repo subpaths and expected
checksums).

### Global options

These apply to every command:

| Option | Description |
|---|---|
| `-q, --quiet` | Suppress non-error output (and use the `quiet` reporter) |
| `-v, --verbose` | Extra diagnostics |
| `-V, --version` | Print the version |

## `downctl probe`

Print size and HTTP status for targets without downloading.

```
downctl probe <target> [<target> ...] [-p PROXY]
```

## `downctl status`

Scan a directory (default the configured `dir`) for in-progress downloads
(piece bitmaps) and the failed log.

```
downctl status [DIR]
```

## `downctl clean`

Remove leftover artifacts (piece bitmaps, the failed log, stale lock
directories) from a directory. The downloaded data files are never touched.

```
downctl clean [DIR] [--dry-run]
```

## `downctl plugin`

```
downctl plugin list          # list discovered plugins by category
downctl plugin info <name>   # show a plugin's category and priority
```

## `downctl config`

```
downctl config path          # print the config file location
downctl config init [--force]# write a default config
downctl config show          # print the effective config
```

## `downctl doctor`

Print version, Python, platform, dependency versions, config path, plugin
count and detected proxy.

## `downctl wizard`

Interactively prompt for targets and a destination, then run `get`.

## Configuration file

`~/.config/downctl/config.toml` (override with `DOWNCTL_CONFIG`). Values
support `${ENV_VAR}` interpolation.

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 0
# connections = 16
# stall = 30
# rate = 5242880
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"

[plugins.s3]
# region = "us-east-1"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# path_style = true

[plugins.sftp]
# user = "me"
# password = "${SFTP_PASSWORD}"
# key = "~/.ssh/id_ed25519"
# known_hosts = "~/.ssh/known_hosts"

[plugins.tqdm]
enabled = true
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more files failed |
| 130 | Interrupted (progress is kept; re-run to resume) |
