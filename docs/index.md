# downctl

A plugin-based universal command-line downloader with resumable,
multi-connection accelerated and self-healing downloads.

> 中文用户请看仓库根目录的 [README.md](https://github.com/ThzxxArt/downctl/blob/master/README.md)（详细中文指南）。

**Everything is a plugin** — sources, transports, verifiers, sinks, reporters,
routers and hooks all share one interface, and the built-in functionality uses
that same interface.

## Install

```bash
pip install downctl
# SFTP support:
pip install 'downctl[sftp]'
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

# An S3 prefix (built-in source; uses AWS credentials)
downctl get s3://my-bucket/datasets/ --include '*.parquet'

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
