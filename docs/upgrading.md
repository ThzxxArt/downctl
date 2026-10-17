# Upgrading

`downctl` uses standard packaging, so upgrading is a normal install/upgrade —
no need to uninstall first.

## pip

```bash
python -m pip install --upgrade downctl          # latest
python -m pip install 'downctl==0.1.1'           # a specific version
python -m pip install --upgrade 'downctl[sftp]'  # include optional extras
```

On PEP 668 systems (Ubuntu 24.04+/Debian 12+) use a virtualenv, `pipx`, or
`--break-system-packages`.

## pipx

```bash
pipx upgrade downctl
pipx upgrade --include-injected downctl
pipx install --force 'downctl[sftp]'             # add extras
```

## pip / editable (from source)

```bash
git pull
python -m pip install --upgrade .                # regular
python -m pip install --upgrade -e '.[dev,sftp]' # editable/dev
```

## Docker

```bash
git pull
docker build -t downctl .
```

## Verify

```bash
downctl version                 # prints the version
pip show downctl | grep Version # or via pip
downctl doctor                  # deps / plugins / proxy
```

## What upgrading does NOT touch

- Your downloaded files
- `~/.config/downctl/config.toml` (and any custom `DOWNCTL_CONFIG`)
- Pending piece bitmaps (`.downctl-pieces`), `.downctl.failed`, lock dirs

## Compatibility notes

- Config keys are backward compatible; unrecognized keys are ignored
  (`downctl config show` prints the effective values).
- Since 0.1.1 the version is sourced from the package's `__version__`; use
  `downctl version` to confirm.
- Plugins declare a plugin-API version (`downctl.API_VERSION`, currently `1`);
  if a future release bumps it, incompatible plugins are rejected with a clear
  message rather than failing later.

## Uninstall

```bash
python -m pip uninstall downctl
# pipx uninstall downctl
```

Your config and downloads are left in place (remove them manually if desired).
