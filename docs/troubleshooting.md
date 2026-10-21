# Troubleshooting

Start with the built-in diagnostics:

```bash
downctl doctor          # version, python, deps, proxy, plugin count
downctl -v get <url>    # verbose (shows a traceback on unexpected errors)
```

## Downloads

**Does an interrupted download restart from zero?**
No. Downloads resume from the piece bitmap; re-run the same command and only
missing pieces are fetched. Multi-connection transfers checkpoint the bitmap
every `--checkpoint` seconds (default 30), so `kill -9` or a power loss loses at
most one interval; `Ctrl+C` and `SIGTERM` (POSIX; Windows console processes have
no catchable SIGTERM) finalize immediately.
Unknown-size or non-Range servers use a single stream with no bitmap, so those
downloads restart from zero (use `--skip-existing` to skip a completed file by
its marker).
(`DOWNCTL_TRUST_PARTIAL=1` only affects the *complete-file* judgement for a
full-size, marker-less local file whose holes can't be probed — it is not for
resuming a smaller-than-expected file.)

**Re-running re-downloads everything?**
No. Completed size-known files carry `<file>.downctl-done` (size + mtime + URL
identity) and are skipped cheaply. A file placed by another tool has no marker:
on native Linux filesystems downctl proves it has no holes; on WSL DrvFS /
Windows it
re-fetches instead of guessing. `--skip-existing` follows the same rule and
re-verifies an explicitly supplied checksum.

**A file with the right size is downloaded again**
That is intentional. A right size does not prove the middle bytes exist
(interrupted multi-connection downloads leave holes). The old file is reset
unless a completion marker, a complete bitmap, or (on native POSIX filesystems)
a hole-free scan proves it. On WSL DrvFS / Windows a marker-less file is
re-fetched; if you know the file is complete and it was produced elsewhere, set
`DOWNCTL_TRUST_PARTIAL=1` for that run.

**A file I already have was re-downloaded**
Three reasons, all of them about proof: (1) the server now names a different
version (its strong/weak `ETag` or `Last-Modified` changed), so the old ledger
and the old completion marker were refused — that is the 0.1.4 fix, not a
regression; (2) `--require-witness` (or `[core] require_witness = true`) refuses
to skip or resume anything whose version cannot be checked, so a server without
validators is re-fetched; (3) `--recheck` re-hashes existing files against their
recorded/source digest before trusting them. Each case only affects that file.

**Switching a revision / mirror re-downloads**
Bitmaps are bound to a URL identity. `hf://repo@v1` and `hf://repo@v2` (or two
same-size URLs) are different objects, so the old ledger is discarded. For S3,
the rotating presigned query is ignored when computing the identity, so
re-signing does not lose progress. Since 0.1.4 the identity is not the whole
story: a URL whose *content* was replaced is detected through the object's
version witness, so a re-uploaded asset that keeps its URL also starts a clean
download instead of resuming into the new revision.

**"disk space" error**
A disk pre-flight (200 MiB headroom) runs before each file, and before the run
starts the engine sums what every target still needs per filesystem: a batch
whose total cannot fit fails up front with one message naming the need and the
free space, and no bytes are written
(`tests/integration/test_resilience.py::test_a_batch_that_cannot_fit_never_starts_writing`).
Concurrency is deliberately not reduced to make a batch fit — bytes already
written stay on disk. Free up space and re-run to resume.

**`--max-time` gave up**
`--max-time SECONDS` caps a single target's wall clock (`0` = no limit). When it
expires the target is failed and recorded in `.downctl.failed`, but the pieces
already downloaded are kept, so re-running resumes
(`tests/integration/test_resilience.py::test_max_time_gives_up_and_records_the_target`).
A server that merely stalls is caught earlier by `--stall`.

**The transfer seems stuck**
A stall watchdog aborts a transfer with no progress for `--stall` seconds and
reconnects. Tune `--stall`, or check the proxy/network.

**`checksum failed N times`**
The file repeatedly mismatches. Check the expected hash (or the source) — a
server that mutates content will never pass.

**"unknown checksum algorithm"**
The algorithm has no verifier. Built in: `sha256`, `sha1`, `sha512`, `md5`.
A typo or a new algorithm fails *before* the download, instead of fetching the
file and failing verification twice.

## Verifying completed files

**`downctl verify` exits 3**
Exit `3` means nothing is *wrong*, but nothing could be *proven*: the completion
marker recorded no digest and the content was not hashed. The marker's size/mtime
binding still catches external edits (that is `mismatch`, exit `1`), but it is not
content proof. Pass `--checksums` with a `sha256sum`-style manifest to turn those
entries into `ok`
(`tests/unit/test_cli_verify.py::test_verify_without_a_digest_is_unverifiable_not_ok`).
`--repair` never deletes an unverifiable file
(`tests/unit/test_cli_verify.py::test_verify_repair_keeps_an_unverifiable_file`).
Exit `1` is a real problem: a `mismatch` (digest or size/mtime disagreement) or a
`missing` data file behind a marker. `2` is reserved for argparse usage errors.

## HTTP / auth

**A probe fails after three attempts**
Probing retries transient failures — `5xx`, `429`, `408`, `425` and network drops
— at most 3 times, with a 5 s cap on both the backoff and any `Retry-After`, then
raises `ProbeError`. The bound holds even with `-r 0`
(`tests/integration/test_resilience.py::test_probe_retries_stay_bounded`). A
transient failure that clears is invisible: two failures followed by success
still downloads the file
(`tests/integration/test_resilience.py::test_a_transient_probe_failure_is_retried`).
Any other 4xx is the server's final answer and is not retried — a `404` fails
immediately and lands in `.downctl.failed`
(`tests/integration/test_resilience.py::test_a_permanent_probe_failure_is_not_retried`).
A `401`/`403` counts as final only if the *ranged `GET`* agrees: a `HEAD` that is
rejected that way is a **method-scoped** answer (a presigned URL signs the HTTP
method), so the probe retries it as a ranged `GET` before giving up.

**401/403/404**
Private resource or anonymous rate limit. Provide a token
(`HF_TOKEN`/`GITHUB_TOKEN` or the plugin `token`). A HuggingFace token is sent
only to the host the source resolved — the Hub, or a configured
`[plugins.huggingface] mirror` and only when `token_to_mirror = true`; a `401`
caused by the withheld token names that key. `--mirror` is a URL prefix rewrite,
not a credential: it does not get you into a private object (a mirror that does
not hold it answers `404`), and on a different host the token travels with the
rewritten request. A `403` on an `s3://` target whose download works is expected:
the signature covers the method, and the probe falls back to a ranged `GET`.

**Proxy / WSL**
Set `-p` or the `https_proxy`/`HTTP_PROXY` env vars. Under WSL the Windows
host proxy is auto-detected; override the host with `WSL_HOST_IP`.
`downctl doctor` prints the detected proxy.

`-p` and the environment variables apply to every request. The auto-detection is
a *guess* — it assumes a proxy on port 7897 at whatever `/etc/resolv.conf` names
as the host — so it applies only to remote targets: a target whose host is
`localhost`, a loopback/link-local/unspecified IP, or anything `no_proxy` covers
goes direct. If nothing is listening there, every remote request goes to a dead
port; to turn the guess off entirely, set `WSL_HOST_IP=127.0.0.1` (that exact
value means "no host proxy"), or pass the proxy you actually use, or pass
`--no-proxy` to ignore every proxy including an exported one.

**Redirects to another host don't carry the token**
Tokens are per-host (HF/GitHub only); the shared session is not given a global
`Authorization`, so a redirect to an unrelated host drops it
(`tests/integration/test_transfer.py::test_authorization_is_dropped_on_cross_origin_redirect`).
An explicit routing decision is different: `--mirror` and `-p` apply to the
request you asked to send, so the token follows it to the rewritten host.

## SFTP

**"Host key is not trusted"**
Host keys are verified against `~/.ssh/known_hosts` by default. Add the host
key, or disable verification with `[plugins.sftp] known_hosts = ""` (also
accepts `none`/`false`).

**"SFTP support requires asyncssh"**
Install the optional extra: `pip install 'downctl[sftp]'`.

## S3

Presigned URLs expire (default 1 h). Very long transfers can outlive them —
raise `[plugins.s3] expires` (max 7 days) or re-run.

`--retry-failed` does not replay the expired URL: v3 failed-log records keep the
original `s3://bucket/key` origin and mint a fresh signature. Records created by
0.1.2 or older have no origin and print a warning; re-run the original `s3://`
target instead.

## JSON / scripting

`--reporter json` emits one object per line on stdout (`run_start`, `start`,
`progress`, `resume`, `finish`, `failure`, `run_finish`). `failure` names
targets that failed during resolve/probe, and `run_finish.failed` includes
them. Two fields exist so a script never has to parse prose:

- `finish.witness` — `validator`, `checksum` or `none`: what the `ok: true`
  claim rests on. A skip whose version could not be checked reports `none`
  (`tests/integration/test_resilience.py::test_a_json_skip_reports_the_witness_it_was_skipped_with`).
- `failure.reason` and `finish.reason` — `permanent`, `transient-exhausted` or
  `timeout`: whether running the target again is worth anything
  (`tests/integration/test_resilience.py::test_json_says_why_a_target_failed`).

Human output is suppressed, warnings/errors stay on stderr, so
`downctl get … --reporter json | jq` works directly.

`downctl probe` exits `1` if any target is unreachable, not HTTP(S), or answers
with HTTP >= 400;
`downctl status --json` and `downctl clean --json` emit one JSON object.

## Cleanup

```bash
downctl status <dir>    # incomplete + failed downloads
downctl clean <dir>     # remove leftover bitmaps / failed log / dead locks
```

`clean` never removes a lock directory that is not marked with `downctl-lock`,
never touches your data files, and keeps completion markers whose data file still
exists (orphan markers are removed).

## Failure reasons

Every failure is recorded in `.downctl.failed` with one of four classes in the
failure-class column (v5 also records header *names*, transport and writer
host), and the same value appears as `reason` in the JSON stream. The class
decides whether re-running can possibly help.

| Class | What it means | Re-run? |
|---|---|---|
| `permanent` | The server gave a definite answer (401/403/404), the source cannot handle the target, or a checksum spec is malformed. Nothing about the bytes will change on the next attempt. | No — fix the target, credentials or checksum first |
| `transient-exhausted` | The probe kept failing (5xx, 429/408/425, a dropped connection) until its bounded retry budget ran out. | Yes — the server may be back |
| `timeout` | The target hit `--max-time`. | Yes, with a larger `--max-time` or a smaller target |
| `unknown` | An older record, or a failure that predates classification. | Treat as `permanent` until re-run proves otherwise |

A `403` on a *presigned* URL is the one case where a re-run is worth trying
without changing anything: the signature expired, and a source that can re-sign
(s3) does so during the same run.

## Platform differences

The same command behaves slightly differently per platform; these are the ones
that change what you see, not what you get.

| Area | Linux / macOS | Windows | WSL |
|---|---|---|---|
| Paths | POSIX paths | `D:\models` works everywhere | `D:\models` is translated to `/mnt/d/models`; `downctl doctor` shows the result |
| File locks | Advisory lock directory with an owner record | Same, and a killed process is detected by its exit code, not by whether it can be opened | Same as Linux |
| Sparse-file proof | `SEEK_HOLE`/`SEEK_DATA` prove a file has no hidden gaps | No cheap hole proof: an unmarked file is not treated as complete | Same as Linux |
| `SIGTERM` | Becomes `KeyboardInterrupt` → exit 130, state kept | No catchable `SIGTERM` for console programs; closing the window ends the process | Same as Linux |
| Proxy autodetection | None | None | The Windows host IP is guessed as a proxy **only for remote targets**; `localhost`, loopback/link-local/unspecified and `no_proxy` matches go direct. `WSL_HOST_IP=127.0.0.1` disables the guess; `--no-proxy` ignores every proxy |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more targets failed; also `status` with failed/blocked entries, `verify` with a mismatch/missing file, and `batch` with a failed job |
| 2 | `batch` only: invalid job file (nothing downloaded). Argparse usage errors also exit 2 |
| 3 | `verify` only: nothing wrong, nothing provable |
| 130 | Interrupted (progress kept; re-run to resume) |
