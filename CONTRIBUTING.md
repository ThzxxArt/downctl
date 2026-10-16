# Contributing to downctl

Thanks for your interest! This document covers the essentials.

## Development setup

```bash
git clone https://github.com/ThzxxArt/downctl
cd downctl
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

## Checks

Run all of them before opening a pull request:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Design principles

- **Everything is a plugin**, except the stateful core (scheduling, piece
  bitmaps, watchdog, rate limiting, locks, disk checks). Keep the core stable;
  add capabilities as plugins.
- Built-in functionality must use the same plugin interface as third-party
  plugins.
- Keep the plugin API versioned (`downctl.API_VERSION`). Any breaking change
  to a plugin contract requires a version bump and a migration note.
- Hot paths (the piece download loop) must not cross plugin boundaries.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## Commit messages

Use clear, imperative subject lines (e.g. `add sha256 verifier plugin`).
Reference issues where relevant.

## License

By contributing you agree your contributions are licensed under the MIT
License.
