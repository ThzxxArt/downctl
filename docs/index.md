# downctl

A plugin-based universal command-line downloader with resumable,
multi-connection accelerated and self-healing downloads.

**Everything is a plugin** — sources, transports, verifiers, reporters, routers,
hooks, job sources, auth providers and servers all share one interface, and the
built-in functionality uses that same interface. (The `sink` category was
removed in 0.1.4: it was never called on the write path.)

- Simple: `downctl get <target>`.
- Reliable: interruptions resume automatically; only missing pieces are
  re-fetched; no silent corruption. The guarantee holds while the server offers
  a usable version witness (`ETag`/`Last-Modified`) or a checksum; with neither,
  a replaced object can cause a stale skip but never a spliced file —
  `--require-witness`, or `downctl verify --checksums`, is how you demand proof.
- Fast: multi-connection per file, multiple files concurrently.
- Universal: HTTP(S), HuggingFace, GitHub Releases, S3, SFTP and local files.
- Extensible: publish your own source, transport, verifier, hook, job source,
  auth provider or server as a plugin.

## Install

```bash
pip install downctl            # from PyPI
pipx install 'downctl[sftp]'   # CLI tool with SFTP support
```

Requires Python 3.11+. Docker:

```bash
docker build -t downctl .
docker run --rm -v "$PWD:/data" downctl get <target>
```

## Quickstart

```bash
# A plain file
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# A HuggingFace repository (only *.safetensors, 2 files at a time)
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# A GitHub release
downctl get gh://cli/cli --include '*.tar.gz'

# An S3 prefix (uses AWS credentials)
downctl get s3://my-bucket/datasets/ --include '*.parquet'

# Probe without downloading
downctl probe https://example.com/big.iso

# Inspect the environment
downctl doctor
downctl plugin list

# Prove (offline) that completed files still match their source
downctl verify ~/Downloads

# Run several groups of targets, each with its own options
downctl batch jobs.toml

# Check which credential a host would use, then hand files to another machine
downctl auth check https://huggingface.co/meta-llama/Llama-3-8B
downctl serve ~/models --port 8080
```

## Sources

| Source | Target form |
|---|---|
| URL | `https://…` |
| HTTP directory | `https://host/dir/` (trailing `/`) |
| HuggingFace | `hf://org/name[@revision]`, bare `org/name` |
| GitHub Releases | `gh://owner/repo[@tag]` |
| S3 | `s3://bucket/key`, `s3://bucket/prefix/` |
| SFTP | `sftp://[user@]host[:port]/path` (needs `downctl[sftp]`) |
| Local file | `file:///path` |

## Configuration

`$XDG_CONFIG_HOME/downctl/config.toml` (or `~/.config/downctl/config.toml`;
override with `DOWNCTL_CONFIG`); values support `${ENV_VAR}`.

```toml
[core]
dir = "~/Downloads"
jobs = 1
retries = 10
connections = 16
stall = 30
checkpoint = 30
# max_time = 0            # wall-clock cap for one target (0 = no limit)
# probe_jobs = 8          # concurrent resolve/probe (0 = follow jobs)
# require_witness = false # refuse to skip/resume state with no checkable version
# piece_size = 8388608
# min_split_size = 1048576
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["logfile", "archive", "webhook", "manifest", "exec", "checksum"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"
# api = "https://api.github.com"

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
# key_passphrase = ""
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

### Environment variables

`DOWNCTL_CONFIG`, `XDG_CONFIG_HOME`, `HF_TOKEN`, `GITHUB_TOKEN`/`GH_TOKEN`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`,
`https_proxy`/`HTTP_PROXY`, `WSL_HOST_IP`, `NO_COLOR`,
`DOWNCTL_TRUST_PARTIAL`, `DOWNCTL_RETRY_BASE`.

## Contents

- [Upgrading](upgrading.md)
- [CLI reference](cli.md)
- [Jobs, credentials and serving](batch.md)
- [Sources](sources.md)
- [Configuration](configuration.md)
- [Hooks](hooks.md)
- [Plugin development](plugins.md)
- [Troubleshooting](troubleshooting.md)
- [Cookbook](cookbook.md)
- [Design](design.md)
- [Security advisory: 0.1.3 splicing](security-advisory-0.1.3-splicing.md)

## License

MIT © ThzxxArt
