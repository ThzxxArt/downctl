# downctl

A plugin-based universal command-line downloader with resumable,
multi-connection accelerated and self-healing downloads.

**Everything is a plugin** — sources, transports, verifiers, sinks, reporters,
routers and hooks all share one interface, and the built-in functionality uses
that same interface.

## Install

```bash
pip install downctl
```

Requires Python 3.11+.

## Quickstart

```bash
# A plain file
downctl get https://example.com/big.iso -d ~/Downloads -t 5M

# A HuggingFace repository (built-in source)
downctl get hf://meta-llama/Llama-3-8B --include '*.safetensors' -j 2

# A GitHub release (built-in source)
downctl get gh://cli/cli --include '*.tar.gz'

# Probe without downloading
downctl probe https://example.com/big.iso

# Inspect the environment and plugins
downctl doctor
downctl plugin list
```

## Contents

- [CLI reference](cli.md)
- [Plugin development](plugins.md)
- [Design](DESIGN.md)

## License

MIT © ThzxxArt
