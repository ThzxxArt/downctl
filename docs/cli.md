# CLI reference

```
downctl [--version] <command> [options]
```

## `downctl get`

Download one or more targets. An HTTP(S) URL is handled by the `url` source;
`hf://org/name[@revision]` (or a bare `org/name`) by the `huggingface` source.

| Option | Description |
|---|---|
| `-d, --dir DIR` | Destination directory |
| `-o, --output PATH` | Save a single download to PATH |
| `-j, --jobs N` | Concurrent files |
| `-t, --limit-rate RATE` | Rate limit (`5M`, `1m`, `512k`) |
| `-r, --retries N` | Max retries per file (`0` = retry forever) |
| `--checksum ALGO:HEX` | Verify after download (e.g. `sha256:...`) |
| `-p, --proxy URL` | Proxy URL |
| `--mirror FROM=TO` | Rewrite a URL prefix before fetching |
| `--source NAME` | Force a source plugin |
| `--reporter NAME` | Progress reporter (`tqdm`, `json`, `quiet`) |
| `--include PAT` | Include globs (repo sources), repeatable |
| `--exclude PAT` | Exclude globs (repo sources), repeatable |
| `--revision REV` | Repo revision |
| `-l, --list FILE` | Read targets from a file (one per line, `#` comments) |
| `--dry-run` | Probe only, do not download |
| `--retry-failed` | Retry targets from the last failed log |
| `--local-plugins` | Also load plugins from local plugin directories |

Downloaded files resume automatically: interrupt and re-run the same command.
A `.downctl.failed` log is written next to the output for `--retry-failed`.

## `downctl probe`

Print size and HTTP status for targets without downloading.

```
downctl probe <target> [<target> ...] [-p PROXY]
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
# rate = 5242880
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.tqdm]
enabled = true
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more files failed |
| 130 | Interrupted (progress is kept; re-run to resume) |
