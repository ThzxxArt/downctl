# Upgrading

`downctl` uses standard packaging, so upgrading is a normal install/upgrade —
no need to uninstall first.

## pip

```bash
python -m pip install --upgrade downctl          # latest
python -m pip install 'downctl==0.1.7'           # a specific version
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

## Upgrading to 0.1.7

"Display truth." The download pipeline, state formats and `API_VERSION` are
unchanged. What changed is what a run shows, and who decides it.

- **The default reporter on a terminal is now `live`.** It draws one aggregate
  bar and one row per working file, with rate, ETA, retry waits and the
  verifying phase; the block is on stderr, the one-line summary stays on
  stdout. `--reporter tqdm` still selects the 0.1.6 bars, and a pipe keeps the
  0.1.6 contract: no progress chatter.
- **`NO_COLOR` no longer disables progress.** It means "no colour" and is
  honoured by its presence; the old selector also used `NO_COLOR=1` to choose
  the `quiet` reporter, so a plain-colour terminal lost every byte count.
- **New `--progress auto|live|plain|off` and `[core] progress`.** `auto` is the
  default; `plain` writes one periodic line and one line per failed/skipped
  file (useful in CI); `off` is the quiet reporter. `[plugins.live]` accepts
  `fps`, `unicode`, `color`, `plain_interval` and `enabled`. An explicit
  `--reporter` still wins over `--progress`.
- **New optional reporter events**: `phase`, `task_phase`, `task_retrying`,
  `task_restarted`, plus `unknown`/`connections` on `run_started`. They are
  feature-detected and signature-filtered; a 0.1.6 reporter needs no change and
  a failing presentation hook disables the display instead of failing a file.
- **Fixes**: the run-level resume seed now obeys `--require-witness` (it could
  count bytes the engine was about to re-download); the tqdm reporter no longer
  leaks a bar across `batch` jobs; `NO_COLOR` has one meaning in the selector
  and in `console`.

`-q` still keeps command results, `--reporter json` still owns stdout, and the
JSON event names and fields are unchanged.

## Upgrading to 0.1.6

"Jobs, identities, delivery." Three new plugin categories and the commands that
use them, plus a set of behaviour fixes. There is **no state migration**:
`BITMAP_VERSION` stays `4`, `COMPLETION_VERSION` stays `2`, the failed log is
extended additively to v6 (older records still load), and `API_VERSION` stays
`"1"`.

New commands and configuration:

- `downctl batch <file>` runs TOML/JSON job files: `--job NAME` (repeatable),
  `--job-source NAME`, `--dry-run`, `--continue`, and every download option as
  a default. Exit `0` all succeeded, `1` a target/job failed, `2` the job file is
  invalid (nothing downloaded). See [Jobs, credentials and serving](batch.md).
- `downctl auth check <url>... [--json]` and the new `[auth."<host>"]` section
  (`env`, `command`, `netrc` providers). Material is only ever sent to the first
  origin of a request; an explicit `-H`/`-u` wins.
- `downctl serve <dir|file>` with `--bind`, `--port`, `--auth`, `--index` and
  `--quiet-logs`.
- `downctl plugin check <module-or-dist>` and
  `downctl plugin new <name> --kind KIND`; the public
  `downctl.plugin.testing` helpers.
- New built-ins: `http_index` source, `metrics` and `notify` hooks, `toml`/`json`
  job sources, `env`/`command`/`netrc` auth providers, `http` server.

Behaviour changes:

- **`--no-proxy`** connects directly, ignoring a configured/environment proxy
  (and the WSL guess). **`--limit-rate 0`** means unlimited and is no longer
  rejected.
- **`downctl status` exits `1`** when there are failed or blocked targets; its
  JSON `failed` entries are objects `{"target", "reason"}` and it includes
  `blocked`.
- **`downctl verify` exits `3`** for "unverifiable" (`2` is reserved for
  argparse usage errors), and marks a marker-carried witness as *recorded, not
  compared*.
- **`-q` keeps the command's result**: `config show`, `status`, `plugin list`
  and friends still print it; only progress chatter is suppressed.
- **`config show` prints the effective configuration** (defaults + file +
  environment) and masks `[auth.*]` values; unknown `[core]` keys are warned
  about.
- **Failed log v6** appends request-header *names* (never values), transport and
  writer host to the v4 failure class, plus a `credentialed` flag for a
  URL/origin whose credential was masked and cannot be recovered by re-resolving
  the origin. `--retry-failed` refuses such a record (the original target or the
  credential must be supplied again) and refuses a record whose recorded headers
  are no longer supplied, instead of replaying either without credentials. A
  presigned S3 URL keeps its `s3://` origin, so it stays replayable and is not
  flagged. A refused record is kept as written.
- **`--require-witness` also refuses** a full-size, hole-free, unmarked file
  (it used to be trusted).
- **Manifest merge**: the `manifest` hook adds entries to an existing manifest
  instead of overwriting it. The `archive` hook no longer re-extracts a skipped
  archive and refuses archives whose declared expansion exceeds `max_entries`
  (200000), `max_total_bytes` (20 GiB) or a 1000x ratio. The `checksum` hook's
  `events` only accepts `on_task_finish`.
- **`doctor`** reports the proxy the run will use plus its source, and lists
  `[auth]` rules. **`probe`** prints `validator=<value> (<kind>)` and, on
  failure, `error=`/`retryable=`.
- Built-ins are now registered by code only, so an installed downctl no longer
  advertises them as entry points. Nine categories are scanned.

## Upgrading to 0.1.5

"Carry": every decision a run makes — which credential goes to which host, which
proxy may reach which target, what a completion claim rests on — now reaches the
line that acts on it, instead of being re-derived (or dropped) on the way.

Three defaults changed. Each is a fix, and each is visible on the first run after
upgrading.

- **The HuggingFace token is no longer sent to a configured `mirror`.** A token
  belongs to the host that issued it, and configuring a mirror was never consent
  to hand it that credential. If your mirror needs authentication, set
  `[plugins.huggingface] token_to_mirror = true`; a 401 caused by the withheld
  token names that key. Downloads from `huggingface.co` itself are unchanged.
- **A *guessed* WSL host proxy no longer applies to local targets.** The WSL
  host-proxy autodetection still fills in a proxy for remote requests, but a
  loopback or link-local target (and anything matching `no_proxy`) now connects
  directly. An explicit `-p`, a `proxy` in the config file, or a proxy from the
  environment keeps its full reach — including to local targets, because that is
  what the user asked for.
- **Evidence hooks no longer vouch for a completion they cannot prove.** The
  `checksum` hook writes no sidecar, and `manifest` lists no entry, when the
  engine reports `witness = none` (a skip that could not be tied to a version).
  Both artifacts are fed back into `--checksums`, so a sidecar for an unverified
  file was a promise nobody had kept.

Other behaviour changes worth knowing:

- **`file://` source onto itself is refused.** `downctl get file:///a/x -d /a`
  (or `-o` naming the source) used to be harmless without `--force` and
  destructive with it — the destination was truncated before the transfer read
  it, which emptied the source. It is now a hard error, and `--dry-run` predicts
  it.
- **Hook options are validated, not blindly assigned.** An unknown key or a
  wrong type is warned about and skipped instead of being silently dropped;
  `events = "on_finish"` (a string) is normalised to a list — as a string it made
  the hook fire on `on_task_finish`, because `"on_finish" in "on_task_finish"` is
  true — and an unknown event name is refused.
- **A failing hook is reported.** A post-processing step that raised (an
  encrypted archive, a corrupt tarball, `timeout = "300"`) was invisible before:
  the exception died with its task handle. It is now a warning, and
  `[core] strict_hooks = true` turns any hook failure into exit code `1`.
- **`probe --json` reports `validator_kind`, not `witness`.** The old field name
  collided with the completion vocabulary: `probe` describes *where the remote
  version came from* (`etag` / `last-modified` / `none`), while `witness`
  elsewhere means *what a completion rests on* (`validator` / `checksum` /
  `none`). Scripts reading `probe --json` must rename the key.
- **Objects stored with `Content-Encoding` download correctly.** A server that
  keeps `Content-Encoding: gzip` on an object regardless of `Accept-Encoding`
  used to fail every range with a length mismatch; the transfer now receives the
  stored bytes exactly as `Content-Length` describes them.
- **`--force` together with `--skip-existing` is refused** instead of one
  silently winning.
- **S3**: a download that outlives its presigned URL is re-signed during the run
  instead of failing with `403`; an `endpoint` mounted under a path
  (`https://gateway.example/s3`) keeps that prefix in the signature and the URL;
  a listing that repeats a continuation token is refused instead of looping.
- **State written by a newer downctl is protected, not just left unread.** If
  you downgrade, a ledger or marker from a newer build is reported and left
  alone — including its data file, which earlier builds truncated. `downctl get`
  refuses that target (`--force` is the explicit way to discard it), `status`
  lists it as `blocked`, and `verify` counts it as unverifiable and exits `3`
  instead of reporting "nothing to verify" and exiting `0`.
- **A re-run keeps what the first run proved.** The completion marker keeps the
  version witness and digest a finished download earned, so a plain second run
  no longer downgrades a proven file to unversioned (and the checksum/manifest
  hooks keep producing their artifacts).
- **A failing hook stays visible with `-q`, and `--retry-failed` is only
  suggested when retrying can help.**
- **`doctor` says where a token came from** (`config` or `env`, and which key),
  which is usually the answer to "why is this a 401" — and now also prints the
  proxy it would use, its source and its scope.

If you downloaded files with 0.1.0–0.1.3 and never verified them, read
[the splicing advisory](security-advisory-0.1.3-splicing.md) before trusting
them.

## Upgrading to 0.1.4

- **Resume ledgers now record a version witness — with no format bump.** A ledger
  also records the remote object's version witness (strong or weak `ETag`, falling
  back to `Last-Modified`). The format versions are deliberately unchanged
  (`BITMAP_VERSION` stays `4`, `COMPLETION_VERSION` stays `2`; the witness is a
  purely additive field), so **downgrading to 0.1.3 is safe**: the older build
  ignores the extra field instead of treating the file as unproven state, and
  truncates nothing. A ledger written by 0.1.3 carries no witness, so when the
  server names a version (the normal case) it is discarded and an **incomplete**
  multi-connection download restarts once from zero; when the server offers no
  witness at all there is nothing to compare and the download resumes exactly as
  0.1.3 did. Completed files have no bitmap and are untouched, so this affects only
  downloads that were still in progress when you upgraded.
- **Completion markers stay compatible — also no format bump.** Markers record
  the witness now (`COMPLETION_VERSION` stays `2`), and a 0.1.3-era marker keeps
  being trusted, so upgrading does **not** force a mass re-download
  (`tests/integration/test_content_truth.py::test_a_legacy_marker_keeps_being_trusted`).
  The next run that takes the target to completion rewrites the marker with the
  witness observed then. A witness-less marker that is only *skipped* again under
  `--skip-existing` is reported as `unversioned` and stays witness-less on purpose
  — stamping today's witness onto a file fetched under an unknown one would hide
  the next replacement
  (`tests/integration/test_content_truth.py::test_unversioned_skip_is_proven_and_reported`).
- **The `sink` plugin category is gone.** `downctl.sinks` is a retired
  entry-point group: an installed package that still declares it gets a warning
  from the loader instead of being ignored silently, and `downctl plugin list`
  no longer shows a `sink` line. `API_VERSION` stays `"1"`, so every other
  category that existed in 0.1.4 (six then; the registry defines nine today)
  keeps working.
- **Retries are finite by default** (`[core] retries = 10`). An unbounded retry
  loop as the default turned a permanently failing server into a silent hang.
  Set `retries = 0` (or `-r 0`) to restore "retry forever"; the resolve/probe
  stage stays bounded either way (at most 3 attempts).
- **A replaced object is detected now.** Where 0.1.3 could resume a file whose
  URL stayed the same but whose content had changed, 0.1.4 discards the ledger
  and re-fetches. That is the fix, not a regression: expect the occasional extra
  download when a server stops offering a witness, or when `--require-witness` /
  `--recheck` is enabled.
- **New options**: `downctl verify [DIR]` (offline proof; exit `1` = a mismatch
  or a missing data file, `3` = nothing provable), `--require-witness`,
  `--recheck`, `--max-time SECONDS`, `--probe-jobs N`. A target group that
  cannot fit on its filesystem is refused before a byte is written there.

## Upgrading to 0.1.3

- **Resume bitmaps are now bound to a URL identity (v4).** A bitmap written by
  0.1.2 or older cannot prove which URL it belongs to, so it is discarded on
  the first resume and that file restarts from zero. Completed files have no
  bitmap, so this change does not discard them. This closed the URL/revision
  dimension — the old ledger could mix pieces from two revisions; the
  object-version dimension (same URL, replaced content) is what 0.1.4 closes
  (see above).
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

- 0.1.6 requires Python 3.11+ and `aiohttp>=3.11` (both are declared runtime
  requirements in `pyproject.toml`); pip upgrades `aiohttp` automatically.
- Config keys are backward compatible; an unrecognized `[core]` key is reported
  as a warning instead of being silently ignored (`downctl config show` prints
  the effective values).
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
