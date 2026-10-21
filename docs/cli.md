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
| `-t, --limit-rate RATE` | Rate limit (`5M`, `1m`, `512k`; `0` = unlimited) |
| `-r, --retries N` | Max retries per file (default 10; `0` = retry forever — probing stays bounded) |
| `--max-time SECONDS` | Wall-clock cap for a single target (`0` = no limit) |
| `--probe-jobs N` | Targets resolved/probed concurrently (default 8; `0` = follow `--jobs`) |
| `--force` | Re-download from scratch, ignoring existing file / resume state. Mutually exclusive with `--skip-existing`: they ask for opposite things, so giving both is refused rather than silently resolved |
| `--skip-existing` | Skip only when completion is provable (marker / complete bitmap / provably hole-free file; a supplied checksum is re-verified). An unversioned skip prints a warning |
| `--require-witness` | Refuse to skip or resume state whose object version cannot be checked, including a marker-less full-size file that merely looks complete |
| `--recheck` | Re-hash existing files against their recorded/source digest before skipping; implies `--require-witness` |
| `--checksum ALGO:HEX` | Verify after download (e.g. `sha256:...`) |
| `--checksums FILE` | Verify against `sha256sum`/`sha1sum`/`sha512sum`/`md5sum`-style file (every target needs an entry) |
| `-p, --proxy URL` | Proxy URL |
| `--no-proxy` | Connect directly, ignoring a configured or environment proxy (and the WSL guess) |
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
markers belonging to another URL are discarded rather than resumed.

Since 0.1.4 the ledger (`.downctl-pieces`) and the marker (`.downctl-done`) also
record the remote object's **version witness** — a strong or weak `ETag`, falling
back to `Last-Modified` —
so a URL whose *content* was replaced is detected too: a ledger with a different
witness is discarded and the object is fetched again from zero, and a marker
with a different witness is refused (`--skip-existing` cannot bless stale
content). Ranged requests carry `If-Range` when a strong witness is known, and
every `206` must answer with a `Content-Range` that covers exactly the requested
window — a response for another window is refused before a single byte is
written. A full body answering a conditional request is resolved with one extra
probe: either the server cannot do ranges (single-stream fallback) or the object
was replaced (one restart from zero; `ContentChangedError`). A server whose
witness changes on every response is detected and that target continues without
one, reported as unversioned. `--require-witness` refuses to skip or resume any
state whose version cannot be checked; `--recheck` re-hashes existing files
against their recorded or source digest before skipping and implies
`--require-witness`. The four cases are covered by
`tests/integration/test_content_truth.py::test_same_revision_resumes_only_the_missing_pieces`,
`tests/integration/test_content_truth.py::test_replaced_object_is_not_spliced`,
`tests/integration/test_content_truth.py::test_inflight_replacement_restarts_instead_of_splicing`
and
`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`.

The witness is an additive field, not a new format: `BITMAP_VERSION` stays `4`
and `COMPLETION_VERSION` stays `2`, so an older build ignores it — **downgrading
to 0.1.3 is safe** and truncates nothing.

With
`--skip-existing`, a checksum supplied by the user (`--checksum`/`--checksums`)
is re-verified before skipping. A checksum supplied by the source (HF LFS,
GitHub digest) is not re-hashed only when the completion marker records the
same digest; a changed source digest, a marker-less complete bitmap or a
hole-scan proof is hashed before being blessed.

Retries are finite by default (`[core] retries = 10`). `-r 0` restores "retry
forever" for the transfer loop, but the resolve/probe stage is always bounded:
at most 3 attempts, and a 5 s cap on both the backoff and any server-supplied
`Retry-After` — a probe must never park a whole batch for the hour a server asks
for. The transfer path honours `Retry-After` up to 300 s
(`tests/integration/test_resilience.py::test_retry_after_is_honoured`). A 4xx
other than `408`/`425`/`429` is the server's final answer and is not retried.
`--max-time` caps a single target's wall clock **including its probe**, so a
server that never answers cannot spend the budget twice
(`tests/integration/test_resilience.py::test_max_time_covers_the_probe_stage_too`).

Targets are expanded and probed with bounded concurrency (`--probe-jobs`,
default 8) instead of one round trip after another, while failure reporting keeps
command-line order
(`tests/integration/test_resilience.py::test_probe_phase_is_concurrent`,
`tests/integration/test_resilience.py::test_failure_order_follows_the_command_line`).
Every HTTP(S) target is probed — including one whose size the source already
reported — because that single request is where the version witness comes from;
without it the transfer would send unconditional ranges and the marker could
never notice a replacement.

Before a run starts, the engine sums what every target still needs per
filesystem (plus a 200 MiB reserve). A batch whose total cannot fit fails up
front with one message naming the need and the free space, and no bytes are
written; concurrency is deliberately not reduced, because bytes already written
stay on disk
(`tests/integration/test_resilience.py::test_a_batch_that_cannot_fit_never_starts_writing`).

`--checksums` fails closed: each requested target must have an entry, and an
empty or unusable manifest is rejected before anything is downloaded. Plain
`*sum` files are understood without an `algo:` prefix; the digest length
selects md5/sha1/sha256/sha512.

Failures are recorded in `.downctl.failed` next to the output; `--retry-failed`
re-resolves the stored original target (refreshing S3 signatures and tokens for
private repositories) and downloads it to the same path. Record format v5 keeps
the failure class, the *names* of the request headers used (values are never
written), the transport and the host that wrote the record. A record whose
header names are no longer supplied by `-H` is refused with a message naming
them, instead of being replayed without a credential and reported as a `401`.
An absolute target recorded on another host is refused too. Because a retry
needs the original URL, this state file can contain a credential from that URL;
it is written with owner-only permissions on POSIX.

By default the `tqdm` reporter is shown on a terminal and a one-line summary is
printed (files succeeded, elapsed time, bytes actually transferred and the
destination directory). `--reporter json` makes stdout pure JSON-lines
(warnings/errors stay on stderr), which also means the human summary is
suppressed. With `--dry-run`, the JSON reporter emits one `dry_run` record per
target (`size`/`done`/`pending`/`witness`).

### Global options

These apply to every command (`-V/--version` is top-level only, equivalent to
`downctl version`):

| Option | Description |
|---|---|
| `-q, --quiet` | Suppress progress chatter (and use the `quiet` reporter). The command's actual result — `config show`, `status`, `plugin list`, … — is still printed |
| `-v, --verbose` | Extra diagnostics |

## `downctl batch`

Run the jobs described by a TOML or JSON file. See
[Jobs, credentials and serving](batch.md) for the file schema and the full
rules.

```
downctl batch <file> [--job NAME]... [--job-source NAME] [--dry-run] [--continue] [get options]
```

- `--job NAME` (repeatable) runs only the named jobs; `--job-source NAME`
  forces a job-source plugin (normally chosen by file extension).
- Every download option of `get` is accepted as the default for all jobs (a job
  may override it). `--dry-run` probes every job and downloads nothing;
  `--continue` keeps later jobs running after one fails.
- Jobs run serially, and every job is parsed and validated before the first
  download starts.
- Exit codes: `0` all succeeded, `1` a target/job failed, `2` the job file is
  invalid (nothing downloaded).

## `downctl probe`

Print size and HTTP status for targets without downloading.

```
downctl probe <target> [<target> ...] [-p PROXY] [--json]
```

Also reports the resolved URL, `Content-Type`, the inferred filename and the
version validator the server offered: the text output includes
`validator=<value> (<kind>)`, and on failure `error=` and `retryable=`.
With `--json`, prints one JSON object per target (`target`/`status`/`size`/
`size_human`/`reachable`/`content_type`/`final_url`/`filename`/`validator`/
`validator_kind`, plus `error`/`retryable` when the probe failed, where
`validator_kind` says *which header* carried the version:
`etag`, `last-modified` or `none`).

> **Two words, two meanings.** `validator_kind` describes the remote object
> (where its version came from). `witness` — reported by `get` and `verify` —
> describes a *completion claim* (`validator`, `checksum` or `none`). They are
> not interchangeable, and a field named `witness` holding
> `etag`/`last-modified` was what made that hard to see.
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
`done`, `pieces`, `percent`, `size`, optional `age_seconds`), `failed` (objects
`{"target", "reason"}`) and `blocked` (targets whose sidecar was written by a
newer downctl). The command exits `1` when there is at least one failed or
blocked target, `0` otherwise.

## `downctl verify`

Prove, offline, that files which claim to be complete really match their source.

```
downctl verify [DIR] [--json] [--checksums FILE] [--repair]
```

Every file under `DIR` carrying a recognized `<file>.downctl-done` marker is
judged and reported with the witness that was used. With `--checksums`, every
manifest entry whose file is present is judged as well, marker or not: a
manifest is an independent claim about what should be there, so it also defines
what gets checked.

| Status | Meaning |
|---|---|
| `ok` | Content was proven: the digest from `--checksums` (or recorded in the marker) matched |
| `mismatch` | A digest did not match, or the file's length changed since the marker was written |
| `unverifiable` | Nothing is wrong, but there is no digest to prove the content (only the marker's size/mtime binding could be checked) |
| `missing` | The marker exists, its data file does not |

Exit codes: `0` — everything checked was proven; `1` — at least one `mismatch`
or `missing`, or the path is not a directory (a typo must not read as
"everything is proven"); `3` — nothing wrong, but at least one `unverifiable`.
`2` is reserved for argparse usage errors, so "could not prove it" has its own
code instead of colliding with a mistyped flag.
Without `--checksums`, a marker that recorded no digest can only be
`unverifiable`, and the command prints a hint. Bitmaps (downloads still in
progress) are listed in the JSON `incomplete` array and never affect the exit
code
(`tests/unit/test_cli_verify.py::test_verify_reports_incomplete_downloads_without_failing`).

`--json` prints a single object: `directory`, `checked`, `ok`, `mismatch`,
`unverifiable`, `missing`, `repaired`, `entries` (each with `target`, `status`,
`witness`, `from_manifest` when the manifest rather than the file claimed
completion, and, when applicable, `algo`/`expected`/`actual`/`detail`) and
`incomplete`.

`verify` never contacts the server. When a marker carries a `validator`, the
text output says so explicitly — `witness=validator (recorded, not compared
with the server)` — instead of presenting a recorded witness as if this run had
checked it.

`--checksums FILE` takes the same `sha256sum`-style manifest as `get`; unlabelled
digests are matched by length to md5/sha1/sha256/sha512, and a manifest entry
wins over the digest recorded in the marker. An empty or unusable manifest is
rejected before anything is checked. An entry is attributed to a file by its
**path** only: a manifest that lists a bare `data.bin` describes the file at the
manifest's root, never a same-named file in a subdirectory, because attributing
one file's digest to another is what made `--repair` delete a file the manifest
never named
(`tests/integration/test_proof_boundaries.py::test_repair_never_deletes_a_file_the_manifest_did_not_name`).
An entry that cannot be attributed unambiguously is reported as `unverifiable`
rather than guessed.

`--repair` **deletes** only what was proven bad — a `mismatch` file with its
marker, or an orphan marker — and is destructive; an `unverifiable` file is
never deleted.

Exit codes and repair are covered by
`tests/unit/test_cli_verify.py::test_verify_without_a_digest_is_unverifiable_not_ok`,
`tests/unit/test_cli_verify.py::test_verify_repair_deletes_only_what_was_proven_bad`
and
`tests/unit/test_cli_verify.py::test_verify_repair_keeps_an_unverifiable_file`.

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
the artifacts. The object reports `directory`, `dry_run`, `items`/`count`, plus
`removed`/`failed`; a removal failure is reported and exits `1` instead of
being counted as success. Only recognized bitmaps, valid orphan completion
markers, temp files and dead lock directories carrying the `downctl-lock`
marker are removed; a user file or directory whose name merely ends in those
suffixes is never touched.

## `downctl serve`

Serve a directory or a single file over HTTP so another machine can fetch it
with resumable range semantics.

```
downctl serve [<dir|file>] [--bind ADDR] [--port N] [--auth USER:PASS] [--index] [--quiet-logs]
```

Defaults: `--bind 127.0.0.1`, `--port 8080`, no authentication, no directory
listing. `GET`/`HEAD`; a single range answers `206`, an unsatisfiable one
`416`, `If-Range`, `ETag` (the `.downctl-done` validator when present) and
`Last-Modified` all work. A non-loopback bind prints a warning. Request logs
omit the query string. Stop with `Ctrl+C`.

## `downctl auth check`

Show which `[auth."<host>"]` rule and provider a URL's host resolves to, and
the **names** of the material (never the values).

```
downctl auth check <url>... [--json]
```

`--json` emits one object per URL (`url`/`host`/`rule`/`provider`/
`provider_kind`/`material`/`error`). The command exits `1` when a provider
fails. See [Jobs, credentials and serving](batch.md#credentials).

## `downctl plugin`

```
downctl plugin list                    # list discovered plugins by category
downctl plugin info <name>             # category, priority, origin, api_version, options, capabilities
downctl plugin check <module-or-dist>  # load a plugin and run the contract checks
downctl plugin new <name> --kind KIND --dir DIR   # scaffold a working package
```

`plugin check` accepts `pkg.module`, `pkg.module:Attr` or an installed
distribution name; it exits `1` on any structural problem. `plugin new` writes
a package whose generated test passes immediately (kinds: `source`,
`transport`, `verifier`, `reporter`, `router`, `hook`, `jobsource`, `auth`,
`server`). `plugin info` shows where the winning registration came from, so two
installed packages with the same plugin name are distinguishable.

Local plugin directories (`./downctl_plugins`,
`$XDG_CONFIG_HOME/downctl/plugins`) are only loaded with `--local-plugins`;
without it, merely listing plugins never executes local code.

## `downctl config`

```
downctl config path          # print the config file location
downctl config init [--force]# write a default config
downctl config show          # print the effective config (secrets masked)
```

`config show` prints the configuration a run will actually use: built-in
defaults, then the file, then environment values and CLI overrides. Unknown
`[core]` keys are reported as warnings instead of being silently dropped, and
`[auth.*]` tables are printed with their secret-looking values masked.

## `downctl doctor`

Print version, Python, platform, dependency versions, config path, plugin
count, the proxy the run will use together with its source and scope, token
origins, and the configured `[auth]` rules.

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

`$XDG_CONFIG_HOME/downctl/config.toml` (`~/.config/downctl/config.toml` when
`XDG_CONFIG_HOME` is unset; override with `DOWNCTL_CONFIG`). Values support
`${ENV_VAR}` interpolation.

```toml
[core]
dir = "~/Downloads"
jobs = 2
retries = 10
# connections = 16
# stall = 30
# checkpoint = 30
# max_time = 0            # wall-clock cap for one target (0 = no limit)
# probe_jobs = 8          # concurrent resolve/probe (0 = follow jobs)
# require_witness = false # refuse to skip/resume state with no checkable version
# rate = 5242880
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }

[auth."huggingface.co"]
# provider = "env"
# env = "HF_TOKEN"

[auth."private.example.com"]
# provider = "command"
# command = "pass show example/token"

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
| 1 | One or more files failed; also `status` with failed/blocked targets, `verify` with a mismatch/missing file, `probe` with an unreachable target, and `batch` with a failed job |
| 2 | `batch` only: the job file is invalid (nothing was downloaded). Argparse usage errors also exit `2` |
| 3 | `verify` only: nothing is wrong, but nothing could be proven |
| 130 | Interrupted; re-run the same command to continue (single-stream downloads restart) |

`downctl probe` exits `1` when any target is unreachable, not HTTP(S), or
answers with HTTP >= 400. `downctl verify` has its own codes: `0` — everything
checked was proven, `1` — a mismatch or a completion marker without its data
file, `3` — nothing wrong but nothing provable. `downctl batch` returns `2` only
for an invalid job file; a job that fails while running returns `1`.
