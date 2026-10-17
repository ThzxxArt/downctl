# Upgrading

`downctl` uses standard packaging, so upgrading is a normal install/upgrade —
no need to uninstall first.

## pip

```bash
python -m pip install --upgrade downctl          # latest
python -m pip install 'downctl==0.1.3'           # a specific version
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

- Your downloaded files at install time (the first run may re-fetch marker-less
  files on Windows / WSL DrvFS; see below)
- `~/.config/downctl/config.toml` (and any custom `DOWNCTL_CONFIG`)
- `.downctl.failed`, lock dirs, and completion markers (`.downctl-done`)

## Upgrading to 0.1.3

- **Resume bitmaps are now bound to a URL identity (v4).** A bitmap written by
  0.1.2 or older cannot prove which URL it belongs to, so it is discarded on
  the first resume and that file restarts from zero. This is deliberate: the
  old ledger could silently mix pieces from two revisions. Completed files have
  no bitmap, so this change does not discard them.
- **`--skip-existing` is stricter.** It no longer skips a file just because its
  size matches; it requires a completion marker, a complete bitmap or a
  provably hole-free file, and re-verifies a supplied checksum.
- **Windows / WSL DrvFS trust is stricter.** A full-size file without a
  `.downctl-done` marker is re-fetched instead of trusted, because those
  filesystems cannot prove the absence of holes. This includes files completed
  by 0.1.2 or older (they have no marker): expect one re-download per such file.
  `DOWNCTL_TRUST_PARTIAL=1` forces trust for one run if you know the file is
  complete.
- **`--reporter json` keeps stdout pure.** Human summaries are suppressed;
  warnings and errors go to stderr.
- **`downctl probe` exits `1`** when a target is unreachable, not HTTP(S), or
  answers with HTTP >= 400.
- **`downctl plugin` no longer executes local plugin directories** unless
  `--local-plugins` is passed.
- **Failed log v3 records the original target**, so `--retry-failed` can
  refresh S3 signatures and private-repo tokens. Old records still load.

## Compatibility notes

- 0.1.3 requires `aiohttp>=3.11`; pip upgrades it automatically. The previous
  floor allowed releases where `--proxy` crashed at session creation.
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
