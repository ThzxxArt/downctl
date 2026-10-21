# Jobs, credentials and serving

`downctl` 0.1.6 adds the two ends of the lifecycle to the plugin system:

- **jobs** — one file describes several groups of targets, each with its own
  destination and options (`downctl batch`);
- **credentials** — a host, not a source plugin, decides where its material
  comes from (`[auth."<host>"]` and `downctl auth check`);
- **serving** — a downloaded tree can be handed to another machine with the
  same range semantics downctl itself speaks (`downctl serve`).

`plugin check` and `plugin new` make the contracts verifiable for third-party
authors. Everything here is additive: `API_VERSION` stays `"1"`, and the state
formats do not change.

## Batch job files

```bash
downctl batch jobs.toml
downctl batch jobs.toml --job llama --job tools
downctl batch jobs.json --dry-run
```

`<file>` is TOML (`.toml`/`.tml`) or JSON (`.json`); the format is chosen by the
`jobsource` plugin that recognises the extension (`--job-source NAME` forces
one). Both formats describe the same schema:

```toml
version = 1

[[job]]
name = "llama"
targets = ["hf://meta-llama/Llama-3-8B@main"]
dir = "models/llama"
include = ["*.safetensors", "*.json"]
checksums = "checksums.txt"

[[job]]
name = "tool"
targets = ["https://example.com/tool.tar.gz"]
checksum = "sha256:9f2c..."
hook = ["archive"]
```

The same document as JSON:

```json
{
  "version": 1,
  "jobs": [
    {
      "name": "llama",
      "targets": ["hf://meta-llama/Llama-3-8B@main"],
      "dir": "models/llama",
      "include": ["*.safetensors", "*.json"],
      "checksums": "checksums.txt"
    }
  ]
}
```

### What a job may say

| Key | Required | Meaning |
|---|---|---|
| `name` | no | Stable identifier (`job-<index>` when omitted); duplicate names are refused |
| `targets` | yes | One target string or a non-empty list, same syntax as `downctl get` |
| *(everything else)* | no | A `get` option, spelled with underscores |

Every **download** option `downctl get` accepts is also available here, **except**
the ones that describe the batch itself. These are refused with an error that
says why:

| Refused key | Why |
|---|---|
| `list` | the batch file *is* the list of targets |
| `retry_failed` | the failed log is per run, not per job |
| `reporter` | one run has one reporter |
| `local_plugins` | local plugin directories are process-wide |

The aliases `checksum_manifest` → `checksums`, `hooks` → `hook`,
`headers` → `header` and `continue` → `continue_` are accepted; giving the same
option under both spellings is an error. `version` is required and must be `1`;
a higher number is refused with a message that it was written by a newer
downctl. Relative `dir` and `checksums` values
are resolved against the job file's own directory, so the file stays
relocatable. Types are checked, not coerced: `force = "false"` is an error, not
a truthy string.

The options given to `downctl batch` are the defaults for every job; a job
overrides only the keys it names. `output` is a job-level option and is allowed
only when that job has exactly one target (the batch command itself has no
`-o`).

### How a batch runs

Jobs run **serially**, in file order, sharing one session, one registry, one
event bus and one failed log. Every job is parsed and validated — including its
checksum manifest — **before the first download starts**, so an invalid last
job cannot surface after the first one already wrote bytes.

- `--job NAME` (repeatable) runs only the named jobs; an unknown name is an
  error.
- `--continue` keeps running later jobs after one fails. Without it, the batch
  stops at the first failed job and reports how many were not started.
- `--dry-run` probes every job and downloads nothing; job-level `dry_run` works
  too.
- `on_start`/`on_finish` payloads carry `"job": <name>`, and the JSON reporter's
  `run_start`/`run_finish` records include the same field.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Every selected job succeeded |
| `1` | A target or job failed (at least one byte may have been written) |
| `2` | The job file is invalid: parse/validation failed, nothing was downloaded |

A failure while *running* a job (a probe `404` without `--continue`, for
example) is exit `1`, not `2`; the invalid-file code is reserved for a document
that could not be turned into jobs at all.

## Credentials

Credentials are configured per **host**, not per source plugin:

```toml
[auth."huggingface.co"]
provider = "env"
env = "HF_TOKEN"
header = "Authorization"
prefix = "Bearer "

[auth."*.corp.example"]
provider = "netrc"

[auth."private.example.com"]
provider = "command"
command = "pass show example/token"
ttl = 300
```

### Matching

1. An exact host wins (`huggingface.co`).
2. Otherwise the longest `*.suffix` wins. A wildcard matches **subdomains
   only**: `*.corp.example` matches `git.corp.example` but not `corp.example`.
3. If nothing matches, no provider material is added.

Defining the same host twice is an error, not "last one wins": two tables
cannot both be the answer.

### Providers

| Provider | Options | Behaviour |
|---|---|---|
| `env` | `env` (name or list), `header`, `prefix` | First non-empty variable wins; every name being unset is an error |
| `command` | `command`, `timeout` (default 10 s), `header`, `prefix` | Runs the command; its **single** non-empty stdout line is the token. A non-zero exit or more than one line is an error |
| `netrc` | `path` (default `~/.netrc`) | HTTP Basic credentials for the matching `machine`; a missing file means "no material" |

`header` defaults to `Authorization`, `prefix` to `Bearer ` (an empty prefix is
allowed). Only the provider names the material — it never decides *who may
receive it*: that is still the per-origin rule in `policy_request`. Material is
resolved for the first origin of a request and dropped as soon as a redirect
leaves that origin. An explicit `-H Authorization: ...` or `-u USER:PASS` wins
over a provider.

`ttl` is in seconds and defaults to `0`, which reuses the material for the whole
run. A command-backed provider that mints short-lived tokens can set
`ttl = 300` to renew it instead.

### Inspecting resolution

```bash
downctl auth check https://huggingface.co/meta-llama/Llama-3-8B
downctl auth check --json https://private.example.com/file.bin
```

The text output names the host, the matched `[auth."<host>"]` rule, the
provider, and the **names** of the material (never the values). `--json` emits
one object per URL with `url`, `host`, `rule`, `provider`, `provider_kind`,
`material` (`headers`/`basic`/`source`) and `error`. The command exits `1` when
a provider fails; errors name the rule and provider, never the value.
`downctl doctor` lists the configured rules and whether each named provider
exists.

## Serving a directory

```bash
downctl serve ~/models --bind 127.0.0.1 --port 8080
downctl serve ./model.bin --auth alice:secret --index --quiet-logs
```

| Option | Default | Meaning |
|---|---|---|
| `--bind ADDR` | `127.0.0.1` | Listen address; a non-loopback bind prints a warning |
| `--port N` | `8080` | Listen port |
| `--auth USER:PASS` | *(none)* | Require HTTP Basic credentials |
| `--index` | off | Render an HTML directory listing; without it a directory is `404` |
| `--quiet-logs` | off | Do not log requests |

A directory is served below its root; a single file is served at `/` and at its
own basename. The HTTP subset is the one a downloader needs:

- `GET`/`HEAD`; a single `Range` answers `206` with `Content-Range`, an
  unsatisfiable one answers `416` with `Content-Range: bytes */<size>`;
  multiple ranges are ignored and answered with `200`.
- `If-Range` is honoured against the current `ETag`/`Last-Modified`.
- `ETag` comes from the `.downctl-done` validator when that marker recorded
  one, otherwise `W/"<size>-<mtime_ns>"`; `Last-Modified` is the file's mtime.
- Basic auth is checked **before** the path is resolved, so a wrong credential
  learns nothing about which files exist.
- Paths are percent-decoded, resolved, and checked against the real root;
  `..` and symlinks leaving the root are `403`.
- A request log line has no query string.

Stop it with `Ctrl+C` (`SIGINT`/`SIGTERM` on POSIX and Windows where
available).

### A round trip

```bash
# machine A
downctl serve ~/models --port 8080

# machine B
downctl get http://127.0.0.1:8080/llama/model.safetensors -d ./copy
downctl verify ./copy
```

Because the server derives its `ETag` from the completion marker, the download
sees the same validator that was originally verified, and resume/`If-Range`
work across the hand-off. On a non-loopback bind, add `--auth` or put the
service behind a proxy you trust.

## Authoring and checking plugins

```bash
downctl plugin new example --kind source --dir .
pip install -e example_plugin
downctl plugin check example_plugin.plugin
downctl plugin check example_plugin          # a distribution name also works
```

`plugin new` writes a working package — `pyproject.toml` with the right
entry-point group, the plugin class, a test and a README. Its generated test
passes before you edit anything. `--kind` is one of `source`, `transport`,
`verifier`, `reporter`, `router`, `hook`, `jobsource`, `auth`, `server`.

`plugin check` loads a module (`pkg.module` or `pkg.module:Attr`) or an
installed distribution, runs the registry's structural checks and reports the
optional capabilities it detects. It exits `1` on any problem, naming what is
missing. The same checks are available to your own test suite from the public
`downctl.plugin.testing` module:

```python
from downctl.plugin.registry import Registry
from downctl.plugin.testing import assert_registers, check_plugin, fake_context

from example_plugin.plugin import ExampleSource


def test_contract() -> None:
    plugin = ExampleSource()
    assert check_plugin(plugin, "source") == []
    assert_registers(Registry(), "source", plugin)
```

`check_plugin`, `capabilities`, `option_names`, `assert_registers` and
`fake_context` are shipped as part of the plugin API.

## See also

- [CLI reference](cli.md) — every command and exit code
- [Configuration](configuration.md) — `[auth]` alongside `[core]` and `[plugins]`
- [Plugins](plugins.md) — the nine categories and packaging
- [Cookbook](cookbook.md) — more end-to-end recipes
