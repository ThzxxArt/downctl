"""The metrics hook plugin: export run totals as a Prometheus textfile.

On ``on_finish`` the hook writes the run's counters to ``path`` (default
``downctl.prom``) in the Prometheus text exposition format, ready for a
``node_exporter`` textfile collector. Counters that the payload does not carry
are written as ``0`` so a scrape never sees a stale file with missing series.

The write is atomic: the text goes to ``<path>.<pid>.tmp``, is flushed and
fsynced, then ``os.replace`` swaps it in. A reader therefore sees either the
previous complete file or the new complete file, never a half-written one.

Options (``[plugins.metrics]``):

- ``path``:   output file (default ``downctl.prom``)
- ``labels``: list of ``name=value`` pairs added to every metric

A failed write is reported with ``always=True`` (so ``-q`` cannot hide it) and
counted on the hook's :class:`HookTasks`, which is what ``core.strict_hooks``
inspects.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

from downctl import console
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION

#: Prometheus label names: ``[a-zA-Z_][a-zA-Z0-9_]*``.
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


class MetricsHook(ConfigurableHook):
    """Write Prometheus metrics for the finished run."""

    name = "metrics"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"path": "str", "labels": "list"}

    def __init__(self, path: str = "downctl.prom", labels: list[str] | None = None) -> None:
        self.path = path
        self.labels = _normalize_labels(labels) if labels is not None else []
        self._tasks = HookTasks(self.name)

    def validate_option(self, key: str, value: object) -> None:
        if key == "labels":
            _normalize_labels(value)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event != "on_finish":
            return
        self._write(self._render(payload))

    async def aclose(self) -> int:
        """Return failures recorded since the last drain."""
        return await self._tasks.aclose()

    def _render(self, payload: Mapping[str, object]) -> str:
        succeeded = _count(payload.get("succeeded"))
        failed = _count(payload.get("failed"))
        hooks_failed = _count(payload.get("hooks_failed"))
        total_bytes = _count(payload.get("bytes"))
        elapsed = _number(payload.get("elapsed"))
        lines = [
            _metric("downctl_run_duration_seconds", elapsed, self.labels),
            _metric("downctl_files_total", succeeded, self.labels, ("outcome", "ok")),
            _metric("downctl_files_total", failed, self.labels, ("outcome", "failed")),
            _metric("downctl_bytes_total", total_bytes, self.labels),
        ]
        reasons = payload.get("failures_by_reason")
        if isinstance(reasons, Mapping) and reasons:
            for reason, count in sorted(
                (str(key), _count(value)) for key, value in reasons.items()
            ):
                lines.append(
                    _metric(
                        "downctl_failures_total",
                        count,
                        self.labels,
                        ("reason", _escape_label(reason)),
                    )
                )
        else:
            # Older callers do not send the breakdown; one series is still
            # better than a made-up "permanent" cause for every failure.
            lines.append(
                _metric("downctl_failures_total", failed, self.labels, ("reason", "unknown"))
            )
        lines.append(_metric("downctl_hook_failures_total", hooks_failed, self.labels))
        return "\n".join(lines) + "\n"

    def _write(self, text: str) -> None:
        target = Path(self.path).expanduser()
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            console.warn(f"metrics: could not write {target}: {exc}", always=True)
            self._tasks.record()
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def _metric(
    name: str,
    value: int | str,
    labels: list[str],
    fixed: tuple[str, str] | None = None,
) -> str:
    """Render one sample line, with the hook labels appended after ``fixed``."""
    pairs: list[str] = []
    if fixed is not None:
        pairs.append(f'{fixed[0]}="{fixed[1]}"')
    for label in labels:
        label_name, _separator, label_value = label.partition("=")
        pairs.append(f'{label_name.strip()}="{_escape_label(label_value)}"')
    suffix = "{" + ",".join(pairs) + "}" if pairs else ""
    return f"{name}{suffix} {value}"


def _escape_label(value: str) -> str:
    """Escape a label value for the Prometheus text format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _count(value: object) -> int:
    """Coerce a payload counter, defaulting to zero for absent/garbage values."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _number(value: object) -> str:
    """Render a numeric payload value without dropping a float's fraction."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "0"


def _normalize_labels(value: object) -> list[str]:
    """Validate ``labels`` as a list of ``name=value`` strings.

    A name is checked against the Prometheus grammar and repeats are refused:
    an illegal name (``1bad=x``, ``a-b=x``) makes the whole textfile invalid,
    so the exporter silently drops *every* series, not just that one.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"labels must be a list of name=value strings, got {type(value).__name__}")
    labels: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"labels must be strings, got {type(item).__name__}")
        name, separator, _rest = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"label {item!r} must be name=value")
        if _LABEL_NAME.fullmatch(name) is None:
            raise ValueError(
                f"label name {name!r} is not a valid Prometheus label "
                "(letters, digits and underscores, not starting with a digit)"
            )
        if name in seen:
            raise ValueError(f"label name {name!r} is given twice")
        if name in {"outcome", "reason"}:
            # The per-sample fixed labels use these names; a user label with
            # the same name produces ``{outcome="ok",outcome="prod"}``, which
            # makes the whole textfile invalid and silently drops every series.
            raise ValueError(f"label name {name!r} is reserved by this hook")
        seen.add(name)
        labels.append(item)
    return labels
