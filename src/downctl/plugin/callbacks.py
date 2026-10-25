"""Call optional plugin hooks without breaking older plugins.

The reporter contract is deliberately additive: 0.1.5 reporters implement
``run_started(total, bytes, jobs)``, and a reporter that has never heard of
``job=`` or ``connections=`` must keep working. :func:`call_optional` passes a
keyword only when the callable declares it (or accepts ``**kwargs``), which is
the same rule the engine used privately for ``run_started``/``run_finished``.

Two variants exist because the failure policies differ:

* :func:`call_optional` propagates an exception. It is used for the existing
  run hooks, whose exceptions have always been visible to the caller.
* :func:`call_optional_safe` reports the failure once and continues. It is used
  for the new presentation-only events (phase, retry, restart, verify), where a
  broken third-party reporter must not fail a download.
"""

from __future__ import annotations

import inspect

from downctl import console

_WARNED: set[tuple[int, str]] = set()


def _accepted(method: object, kwargs: dict[str, object]) -> dict[str, object]:
    if not kwargs:
        return {}
    try:
        parameters = inspect.signature(method).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - builtins are introspectable
        return dict(kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


def call_optional(target: object | None, name: str, *args: object, **kwargs: object) -> bool:
    """Call ``target.name(...)`` when it exists; return whether it did."""
    if target is None:
        return False
    method = getattr(target, name, None)
    if not callable(method):
        return False
    method(*args, **_accepted(method, kwargs))
    return True


def call_optional_safe(target: object | None, name: str, *args: object, **kwargs: object) -> bool:
    """Like :func:`call_optional`, but a failure is reported, not raised."""
    if target is None:
        return False
    method = getattr(target, name, None)
    if not callable(method):
        return False
    try:
        method(*args, **_accepted(method, kwargs))
    except Exception as exc:  # noqa: BLE001 - a display must not fail a download
        key = (id(target), name)
        if key not in _WARNED:
            _WARNED.add(key)
            console.warn(
                f"reporter hook {name}() failed: {type(exc).__name__}: {exc}; "
                "the display is disabled for this run"
            )
        return False
    return True


__all__ = ["call_optional", "call_optional_safe"]
