# Troubleshooting

Start with the built-in diagnostics:

```bash
downctl doctor          # version, python, deps, proxy, plugin count
downctl -v get <url>    # verbose (shows a traceback on unexpected errors)
```

## Downloads

**Does an interrupted download restart from zero?**
No. Downloads resume from the piece bitmap; re-run the same command and only
missing pieces are fetched. (`DOWNCTL_TRUST_PARTIAL=1` only affects the
*complete-file* judgement for a full-size local file whose holes can't be
probed — it is not for resuming a smaller-than-expected file.)

**Re-running re-downloads everything?**
Completed files are skipped (bitmap ledger, or size/checksum match).

**"disk space" error**
A disk pre-flight (200 MB headroom) runs before each file; only that file
fails. Free up space and re-run to resume.

**The transfer seems stuck**
A stall watchdog aborts a transfer with no progress for `--stall` seconds and
reconnects. Tune `--stall`, or check the proxy/network.

**`checksum failed N times`**
The file repeatedly mismatches. Check the expected hash (or the source) — a
server that mutates content will never pass.

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

## Cleanup

```bash
downctl status <dir>    # incomplete + failed downloads
downctl clean <dir>     # remove leftover bitmaps / failed log / dead locks
```

`clean` never removes lock directories it cannot prove are dead, and never
touches your data files.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more targets failed |
| 130 | Interrupted (progress kept; re-run to resume) |
