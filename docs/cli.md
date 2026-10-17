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
| `--checkpoint N` | Seconds between durable resume snapshots (default 30; `0` = only on exit) |
| `--piece-size SIZE` | Piece size (`8M`, `4M`; default auto) |
| `--min-split-size SIZE` | Files smaller than SIZE use one connection |
| `-t, --limit-rate RATE` | Rate limit (`5M`, `1m`, `512k`) |
| `-r, --retries N` | Max retries per file (`0` = retry forever) |
| `--force` | Re-download from scratch, ignoring existing file / resume state |
| `--skip-existing` | Skip only when completion is provable (marker / complete bitmap / provably hole-free file) |
| `--checksum ALGO:HEX` | Verify after download (e.g. `sha256:...`) |
| `--checksums FILE` | Verify against `sha256sum`/`sha1sum`/`sha512sum`/`md5sum`-style file (every target needs an entry) |
| `-p, --proxy URL` | Proxy URL |
| `-H, --header K:V` | Extra request header (repeatable) |
| `-u, --user USER:PASS` | HTTP Basic auth |
| `--mirror FROM=TO` | Rewrite a URL prefix before fetching |
| `--source NAME` | Force a source plugin |
| `--reporter NAME` | Progress reporter (`tqdm`, `json`, `quiet`) |
| `--include PAT` | Include globs (repo sources), repeatable |
| `--exclude PAT` | Exclude globs (repo sources), repeatable |
| `--revision REV` | Repo revision |
| `-l, --list FILE` | Read targets from a file (`-` = stdin) |
| `--dry-run` | Probe only, do not download (and do not write or modify download state) |
| `--retry-failed` | Retry targets from the last failed log (re-resolved from the stored origin) |
| `--continue` | Keep going when a target cannot be resolved or probed |
| `--hook NAME` | Enable a hook plugin for this run (repeatable) |
| `--user-agent UA` | Custom HTTP User-Agent |
| `--local-plugins` | Also load plugins from local plugin directories |

Downloaded files resume automatically: interrupt and re-run the same command.
A multi-connection transfer snapshots its bitmap every `--checkpoint` seconds
(default 30), so even `SIGKILL` or a power loss loses at most one interval;
`Ctrl+C` and `SIGTERM` (POSIX) snapshot immediately. Windows console processes
have no catchable `SIGTERM`.

Completion is recorded in `<file>.downctl-done` (size + mtime + URL identity,
plus the verified digest when one was used).
That marker is what lets a later run skip a finished **size-known** file
without trusting its size, including on WSL DrvFS and Windows where holes
cannot be detected. Unknown-size or non-Range single-stream downloads have no
bitmap and restart by default; pass `--skip-existing` to skip one by its
marker. A marker is invalidated when the file is modified externally, and bitmaps or
markers belonging to another URL are discarded rather than resumed. With
`--skip-existing`, a checksum supplied by the user (`--checksum`/`--checksums`)
is re-verified before skipping. A checksum supplied by the source (HF LFS,
GitHub digest) is not re-hashed only when the completion marker records the
same digest; a changed source digest, a marker-less complete bitmap or a
hole-scan proof is hashed before being blessed.

`--checksums` fails closed: each requested target must have an entry, and an
empty or unusable manifest is rejected before anything is downloaded. Plain
`*sum` files are understood without an `algo:` prefix; the digest length
selects md5/sha1/sha256/sha512.

Failures are recorded in `.downctl.failed` next to the output; `--retry-failed`
re-resolves the stored original target (refreshing S3 signatures and tokens for
private repositories) and downloads it to the same path. Records written by
0.1.2 or older have no origin and fall back to the stored URL, with a warning
when that URL may have expired. Because a retry needs the original URL, this
state file can contain a credential from that URL; it is written with
owner-only permissions on POSIX.

By default the `tqdm` reporter is shown on a terminal and a one-line summary is
printed (files succeeded, elapsed time, bytes actually transferred and the
destination directory). `--reporter json` makes stdout pure JSON-lines
(warnings/errors stay on stderr), which also means the human summary is
suppressed. With `--dry-run`, the JSON reporter emits one `dry_run` record per
target (`size`/`done`/`pending`).

### Global options

These apply to every command (`-V/--version` is top-level only, equivalent to
`downctl version`):

| Option | Description |
|---|---|
| `-q, --quiet` | Suppress non-error output (and use the `quiet` reporter) |
| `-v, --verbose` | Extra diagnostics |

## `downctl probe`

Print size and HTTP status for targets without downloading.

```
downctl probe <target> [<target> ...] [-p PROXY] [--json]
```

Also reports the resolved URL, `Content-Type` and inferred filename.
With `--json`, prints one JSON object per target (`target`/`status`/`size`/
`size_human`/`reachable`/`content_type`/`final_url`/`filename`).
The command exits `1` when any target is unreachable, is not an HTTP(S) URL,
or answers with HTTP >= 400; `--json` records include an `error` for the
unsupported case and always carry the real `status`.

## `downctl status`

Scan a directory (default the configured `dir`) for in-progress downloads
(piece bitmaps) and the failed log. Each incomplete entry shows its progress,
size and last-modified age.

```
downctl status [DIR] [--json]
```

`--json` prints a single object: `directory`, `incomplete` (with `target`,
`done`, `pieces`, `percent`, `size`, optional `age_seconds`) and `failed`.

## `downctl clean`

Remove leftover artifacts (piece bitmaps, the failed log, stale lock
directories) from a directory. The downloaded data files are never touched.
Completion markers (`.downctl-done`) are state, not garbage, and are kept while
their data file exists; recognized orphan markers whose file is gone are
removed. A malformed file that merely ends in the suffix is left alone, since
it may be user data.

```
downctl clean [DIR] [--dry-run] [--json]
```

`--json` only changes the output format: without `--dry-run` it still removes
the artifacts. The object reports `items`/`count`, plus `removed`/`failed`;
a removal failure is reported and exits `1` instead of being counted as
success. Only recognized bitmaps, valid orphan completion markers, temp files
and dead lock directories are removed; a user file or directory whose name
merely ends in those suffixes is never touched.

## `downctl plugin`

```
downctl plugin list          # list discovered plugins by category
downctl plugin info <name>   # show a plugin's category and priority
```

Local plugin directories (`./downctl_plugins`, `~/.config/downctl/plugins`)
are only loaded with `--local-plugins`; without it, merely listing plugins never
executes local code.

## `downctl config`

```
downctl config path          # print the config file location
downctl config init [--force]# write a default config
downctl config show          # print the effective config (secrets masked)
```

## `downctl doctor`

Print version, Python, platform, dependency versions, config path, plugin
count and detected proxy.

## `downctl wizard`

Interactively prompt for targets and a destination, then run `get`.

## `downctl completions`

Emit a shell completion script (no extra dependency):

```
downctl completions bash|zsh|fish
# bash: downctl completions bash >> ~/.bashrc
# zsh:  downctl completions zsh  > "${fpath[1]}/_downctl"
```

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
# checkpoint = 30
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
