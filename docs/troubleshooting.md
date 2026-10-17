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

**Switching a revision / mirror re-downloads**
Bitmaps are bound to a URL identity. `hf://repo@v1` and `hf://repo@v2` (or two
same-size URLs) are different objects, so the old ledger is discarded. For S3,
the rotating presigned query is ignored when computing the identity, so
re-signing does not lose progress.

**"disk space" error**
A disk pre-flight (200 MB headroom) runs before each file; only that file
fails. Free up space and re-run to resume.

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

## HTTP / auth

**401/403/404**
Private resource or anonymous rate limit. Provide a token
(`HF_TOKEN`/`GITHUB_TOKEN` or the plugin `token`) or use `--mirror`.

**Proxy / WSL**
Set `-p` or the `https_proxy`/`HTTP_PROXY` env vars. Under WSL the Windows
host proxy is auto-detected; override the host with `WSL_HOST_IP`.
`downctl doctor` prints the detected proxy.

**Redirects to another host don't carry the token**
Tokens are per-host (HF/GitHub only); the shared session is not given a global
`Authorization`, so credentials are never sent to unrelated hosts.

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
them. Human output is suppressed, warnings/errors stay on stderr, so
`downctl get … --reporter json | jq` works directly.

`downctl probe` exits `1` if any target is unreachable, not HTTP(S), or answers
with HTTP >= 400;
`downctl status --json` and `downctl clean --json` emit one JSON object.

## Cleanup

```bash
downctl status <dir>    # incomplete + failed downloads
downctl clean <dir>     # remove leftover bitmaps / failed log / dead locks
```

`clean` never removes lock directories it cannot prove are dead, never touches
your data files, and keeps completion markers whose data file still exists
(orphan markers are removed).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more targets failed |
| 130 | Interrupted (progress kept; re-run to resume) |
