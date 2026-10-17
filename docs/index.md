# downctl

A plugin-based universal command-line downloader with resumable,
multi-connection accelerated and self-healing downloads.

**Everything is a plugin** — sources, transports, verifiers, sinks, reporters,
routers and hooks all share one interface, and the built-in functionality uses
that same interface.

- Simple: `downctl get <target>`.
- Reliable: interruptions resume automatically; only missing pieces are
  re-fetched; no silent corruption.
- Fast: multi-connection per file, multiple files concurrently.
- Universal: HTTP(S), HuggingFace, GitHub Releases, S3, SFTP and local files.
- Extensible: publish your own source/transport/verifier/hook as a plugin.

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
```

## Sources

| Source | Target form |
|---|---|
| URL | `https://…` |
| HuggingFace | `hf://org/name[@revision]`, bare `org/name` |
| GitHub Releases | `gh://owner/repo[@tag]` |
| S3 | `s3://bucket/key`, `s3://bucket/prefix/` |
| SFTP | `sftp://[user@]host[:port]/path` (needs `downctl[sftp]`) |
| Local file | `file:///path` |

## Configuration

`~/.config/downctl/config.toml` (override with `DOWNCTL_CONFIG`); values
support `${ENV_VAR}`.

```toml
[core]
dir = "~/Downloads"
jobs = 1
retries = 0
connections = 16
stall = 30
# rate = 5242880
# proxy = "http://127.0.0.1:7897"
# mirror = { "https://huggingface.co" = "https://hf-mirror.com" }
# hooks = ["logfile", "archive", "webhook", "manifest"]

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

`DOWNCTL_CONFIG`, `HF_TOKEN`, `GITHUB_TOKEN`/`GH_TOKEN`,
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`,
`https_proxy`/`HTTP_PROXY`, `WSL_HOST_IP`, `NO_COLOR`,
`DOWNCTL_TRUST_PARTIAL`, `DOWNCTL_RETRY_BASE`.

## Contents

- [CLI reference](cli.md)
- [Plugin development](plugins.md)
- [Design](design.md)

## License

MIT © ThzxxArt
