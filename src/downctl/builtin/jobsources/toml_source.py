"""The TOML job-file source.

Reads a ``version = 1`` document with one or more ``[[job]]`` tables::

    version = 1

    [[job]]
    name = "llama-3-8b"
    targets = ["hf://meta-llama/Llama-3-8B@main"]
    dir = "models/llama"
    include = ["*.safetensors", "*.json"]
    checksum_manifest = "checksums.txt"

    [[job]]
    name = "tool"
    targets = ["https://example.com/tool.tar.gz"]
    checksum = "sha256:9f2c..."
    hooks = ["archive"]

A job name defaults to ``job-<1-based index>``; duplicate names are refused
because ``--job NAME`` has to identify exactly one job. Option keys are the
long names of ``downctl get`` (underscore form). The older spellings
``checksum_manifest``, ``hooks``, ``headers`` and ``continue`` are normalized to
``checksums``, ``hook``, ``header`` and ``continue_``. Relative ``dir`` and
``checksums`` values are resolved against the job file's own directory, so a
job file stays relocatable.

Every type is checked rather than coerced: ``force = "false"`` is an error,
not a truthy string, and an integer option given a boolean is refused. A typo
in an option name is reported before any download starts, listing the keys a
job may use.

This module also hosts :func:`_read_jobs`, the format-independent half of the
reader. :mod:`downctl.builtin.jobsources.json_source` decodes JSON into the
same mapping and reuses the same validation, so the two formats cannot drift
apart on what a valid job is.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from downctl.core.errors import DownctlError
from downctl.core.job import ALLOWED_JOB_OPTIONS, REFUSED_JOB_OPTIONS, Job
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Spellings accepted in a job file but normalized to the CLI option names.
_ALIASES: dict[str, str] = {
    "checksum_manifest": "checksums",
    "continue": "continue_",
    "headers": "header",
    "hooks": "hook",
}

#: Options whose value must be a real boolean. ``bool("false")`` is ``True``,
#: so accepting the string would silently turn ``force = "false"`` into a
#: re-download; the type is checked instead of coerced.
_BOOL_OPTIONS: frozenset[str] = frozenset(
    {"force", "skip_existing", "require_witness", "recheck", "dry_run", "continue_"}
)

#: Options whose value must be an integer (and not a boolean, which is an
#: ``int`` in Python). ``jobs`` additionally has to be positive.
_INT_OPTIONS: frozenset[str] = frozenset(
    {"jobs", "connections", "stall", "checkpoint", "max_time", "probe_jobs", "retries"}
)

#: Options that accept one string or a list of strings.
_LIST_OPTIONS: frozenset[str] = frozenset({"include", "exclude", "hook", "header"})


def _matches_format(path: str, formats: tuple[str, ...]) -> bool:
    """Whether ``path`` ends in one of ``formats``, case-insensitively."""
    lowered = path.lower()
    return any(lowered.endswith(f".{fmt}") for fmt in formats)


def _describe(value: object) -> str:
    """A short, precise description of a rejected value for an error message."""
    if isinstance(value, bool):
        return f"the boolean {value!r}"
    if isinstance(value, str):
        return f"the string {value!r}"
    if isinstance(value, int):
        return f"the integer {value!r}"
    if isinstance(value, float):
        return f"the float {value!r}"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, Mapping):
        return "a table"
    return f"{type(value).__name__} {value!r}"


def _no_jobs_hint(kind: str) -> str:
    if kind == "TOML":
        return "expected at least one [[job]] table"
    return 'expected a non-empty "jobs" array'


def _check_version(data: Mapping[str, Any], path: str) -> None:
    """Refuse a missing, foreign or future ``version`` before reading jobs."""
    if "version" not in data:
        raise DownctlError(f"{path}: missing 'version' (supported: 1)")
    raw = data["version"]
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw == 1:
            return
        if raw > 1:
            raise DownctlError(
                f"{path}: version {raw} was written by a newer downctl "
                "(this build supports version 1)"
            )
    raise DownctlError(f"{path}: unsupported version {_describe(raw)} (supported: 1)")


def _job_name(entry: Mapping[str, Any], index: int, path: str) -> str:
    """The job's explicit name, or ``job-<index>`` when it has none."""
    raw = entry.get("name")
    if raw is None:
        return f"job-{index}"
    if isinstance(raw, str) and raw.strip():
        return raw
    raise DownctlError(
        f"{path}: job #{index}: 'name' must be a non-empty string, not {_describe(raw)}"
    )


def _targets(value: object, where: str) -> list[str]:
    """A single target string or a non-empty list of non-empty strings."""
    if isinstance(value, str):
        targets = [value]
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                raise DownctlError(
                    f"{where}: 'targets' must contain only strings, found {_describe(item)}"
                )
        targets = list(value)
    else:
        raise DownctlError(
            f"{where}: 'targets' must be a string or a list of strings, not {_describe(value)}"
        )
    if not targets or any(not target.strip() for target in targets):
        raise DownctlError(f"{where}: 'targets' must name at least one non-empty target")
    return targets


def _string_list(key: str, value: object, where: str) -> list[str]:
    """A single string or a list of strings, normalized to a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                raise DownctlError(
                    f"{where}: option {key!r} must contain only strings, found {_describe(item)}"
                )
        return list(value)
    raise DownctlError(
        f"{where}: option {key!r} must be a string or a list of strings, not {_describe(value)}"
    )


def _option_value(key: str, value: object, where: str) -> object:
    """Check one option's value and return what the engine will see."""
    if key in _BOOL_OPTIONS:
        if not isinstance(value, bool):
            raise DownctlError(
                f"{where}: option {key!r} must be a boolean (true or false), not {_describe(value)}"
            )
        return value
    if key in _INT_OPTIONS:
        if isinstance(value, bool) or not isinstance(value, int):
            kind = "a positive integer" if key == "jobs" else "an integer"
            raise DownctlError(f"{where}: option {key!r} must be {kind}, not {_describe(value)}")
        if key == "jobs" and value <= 0:
            raise DownctlError(
                f"{where}: option 'jobs' must be a positive integer, not {_describe(value)}"
            )
        return value
    if key in _LIST_OPTIONS:
        return _string_list(key, value, where)
    if not isinstance(value, str):
        raise DownctlError(f"{where}: option {key!r} must be a string, not {_describe(value)}")
    return value


def _resolve_against(value: str, directory: str) -> str:
    """Resolve a relative path against the job file's directory."""
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(directory, value))


def _read_jobs(document: object, path: str, *, kind: str) -> list[Job]:
    """Validate a parsed TOML or JSON document and return its jobs.

    ``kind`` only labels messages (``"TOML"`` or ``"JSON"``): both formats
    describe the same schema, so they share one reader.
    """
    if not isinstance(document, Mapping):
        expected = "table" if kind == "TOML" else "object"
        raise DownctlError(f"{path}: expected a {kind} {expected} at the top level")
    data: Mapping[str, Any] = document

    _check_version(data, path)

    if "jobs" in data and "job" in data:
        raise DownctlError(f"{path}: give either 'jobs' or 'job', not both")
    key = "jobs" if "jobs" in data else "job"
    if key not in data:
        raise DownctlError(f"{path}: no jobs defined ({_no_jobs_hint(kind)})")
    raw_jobs = data[key]
    if not isinstance(raw_jobs, list):
        raise DownctlError(
            f"{path}: {key!r} must be a list of job tables, not {_describe(raw_jobs)}"
        )
    if not raw_jobs:
        raise DownctlError(f"{path}: no jobs defined ({_no_jobs_hint(kind)})")

    base = os.path.dirname(os.path.abspath(path))
    origin = os.path.abspath(path)
    jobs: list[Job] = []
    seen: dict[str, int] = {}
    for index, raw_entry in enumerate(raw_jobs, start=1):
        if not isinstance(raw_entry, Mapping):
            raise DownctlError(f"{path}: job #{index} must be a table, not {_describe(raw_entry)}")
        entry: Mapping[str, Any] = raw_entry
        name = _job_name(entry, index, path)
        where = f"{path}: job #{index} {name!r}"

        if name in seen:
            raise DownctlError(
                f"{path}: duplicate job name {name!r} (jobs #{seen[name]} and #{index})"
            )
        seen[name] = index

        if "targets" not in entry:
            raise DownctlError(f"{where}: missing required 'targets'")
        targets = _targets(entry["targets"], where)

        options: dict[str, object] = {}
        spelled: dict[str, str] = {}
        for raw_key, raw_value in entry.items():
            if raw_key in ("name", "targets"):
                continue
            option = _ALIASES.get(raw_key, raw_key)
            if option in spelled:
                raise DownctlError(
                    f"{where}: option {option!r} given twice "
                    f"(as {spelled[option]!r} and {raw_key!r})"
                )
            spelled[option] = raw_key
            if option in REFUSED_JOB_OPTIONS:
                raise DownctlError(
                    f"{where}: option {raw_key!r} is refused: {REFUSED_JOB_OPTIONS[option]}"
                )
            if option not in ALLOWED_JOB_OPTIONS:
                allowed = ", ".join(sorted(ALLOWED_JOB_OPTIONS))
                raise DownctlError(f"{where}: unknown option {raw_key!r} (allowed: {allowed})")
            options[option] = _option_value(option, raw_value, where)

        for path_key in ("dir", "checksums"):
            value = options.get(path_key)
            if isinstance(value, str):
                options[path_key] = _resolve_against(value, base)

        jobs.append(Job(name=name, targets=targets, options=options, origin=origin))
    return jobs


class TomlJobSource:
    """Read TOML batch files into the jobs they describe."""

    name = "toml"
    api_version = API_VERSION
    priority = 0
    formats = ("toml", "tml")

    def can_handle(self, path: str) -> bool:
        return _matches_format(path, self.formats)

    async def load(self, path: str, ctx: RuntimeContext) -> list[Job]:
        try:
            with open(path, "rb") as handle:
                document = tomllib.load(handle)
        except OSError as exc:
            raise DownctlError(f"cannot read job file {path}: {exc}") from exc
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise DownctlError(f"{path}: invalid TOML: {exc}") from exc
        return _read_jobs(document, path, kind="TOML")
