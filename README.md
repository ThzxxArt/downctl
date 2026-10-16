# downctl

[![CI](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ThzxxArt/downctl/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/downctl)](https://pypi.org/project/downctl/)
[![Python versions](https://img.shields.io/pypi/pyversions/downctl)](https://pypi.org/project/downctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> A plugin-based universal command-line downloader with resumable,
> multi-connection accelerated and self-healing downloads.

**Everything is a plugin.**

`downctl` downloads files reliably from almost anywhere. Resumable piece
bitmaps, multi-connection acceleration, stall detection and automatic retries
are built into the stable core; everything else — sources, transports,
verifiers, sinks, reporters, routers and lifecycle hooks — is a plugin, and
the built-in functionality uses the exact same plugin interface.

> Status: **pre-1.0**. The download core (resumable, multi-connection) and
> the CLI work end to end. HuggingFace repository expansion and more sources
> are in progress. See [`docs/DESIGN.md`](docs/DESIGN.md) for the design.

## Install

```bash
pip install downctl
```

Requires Python 3.11+.

## Usage

```bash
# Download a file
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# Download a HuggingFace repository (built-in source plugin)
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# Probe without downloading
downctl probe https://example.com/big.iso

# Inspect plugins and environment
downctl plugin list
downctl doctor
```

## Plugins

| Category | Purpose | Built-ins |
|---|---|---|
| Source | Resolve input into tasks | `url`, `huggingface`, `github` |
| Transport | Transfer bytes | `http` |
| Verifier | Check digests | `sha256` |
| Sink | Persist bytes | `filesystem` |
| Reporter | Progress & logs | `tqdm`, `json`, `quiet` |
| Router | Rewrite URLs | `mirror` |
| Hook | Lifecycle events | `logfile` |

Plugins are discovered from three places: built-ins, installed packages
advertising `downctl.*` entry-points, and local directories
(`./downctl_plugins`, `~/.config/downctl/plugins`).

### Write a plugin

See [`examples/plugins/`](examples/plugins) for a minimal installable plugin
package. A plugin is any object exposing the metadata (`name`, `api_version`,
`priority`) and the methods of one of the contracts in
[`downctl.plugin.protocols`](src/downctl/plugin/protocols.py).

> **Security:** local-directory plugins execute arbitrary code. Only enable
> directories you trust; entry-point plugins are loaded automatically.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check .
mypy
pytest
```

## Documentation

- [CLI reference](docs/cli.md)
- [Plugin development](docs/plugins.md)
- [Design](docs/DESIGN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Releasing](RELEASING.md)

## License

[MIT](LICENSE) © ThzxxArt
