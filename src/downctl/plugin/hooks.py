"""Lifecycle event bus for Hook plugins.

The engine emits a small, fixed set of events. Hook plugins subscribe to the
ones they care about. Events are delivered synchronously and any exception a
hook raises is contained so it can never break a download — but never silently:
a failure is reported, counted, and (when ``strict_hooks`` is on) reflected in
the exit code.

The same rule applies to the work hooks hand off to a thread:
:class:`HookTasks` owns those tasks and reads their outcome, because a task that
is only remembered in order to be forgotten takes its exception with it.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, ClassVar, Literal

from downctl import console
from downctl.plugin.protocols import Hook
from downctl.urls import redact_text

#: Canonical lifecycle events.
EVENTS = (
    "on_start",  # payload: {"targets": [...]}
    "on_task_start",  # payload: {"task": DownloadTask}
    "on_task_finish",  # payload: {"task": DownloadTask, "ok": bool, "witness": str}
    "on_error",  # payload: {"task": DownloadTask, "error": str}
    "on_finish",  # payload: {"succeeded": int, "failed": int, "hooks_failed": int}
)

#: The value shapes a hook may declare in :attr:`ConfigurableHook.OPTIONS`.
OptionKind = Literal["str", "int", "bool", "events"]

Handler = Callable[[dict[str, object]], None]


class ConfigurableHook:
    """Validates ``[plugins.<name>]`` keys against what the hook declares.

    Assignment used to be the whole mechanism: ``_enable_hooks`` called
    ``setattr`` for every key the hook happened to have, so a misspelled key was
    dropped by ``hasattr`` without a word. Worse, ``events = "on_finish"`` (a
    string where a list was expected) replaced the list, and the containment
    test became a *substring* test — ``"on_finish" in "on_task_finish"`` is true,
    so the hook fired on an event it was never configured for.

    A hook declares ``OPTIONS`` (name -> kind) and gets normalization plus an
    explicit problem list instead.
    """

    #: Option name -> kind. Kinds: ``str``, ``int``, ``bool``, ``events``.
    OPTIONS: ClassVar[Mapping[str, OptionKind]] = {}

    def configure(self, options: Mapping[str, object]) -> list[str]:
        """Apply ``options``; return the problems found (empty when clean)."""
        problems: list[str] = []
        for key, value in options.items():
            kind = self.OPTIONS.get(key)
            if kind is None:
                known = ", ".join(sorted(self.OPTIONS)) or "none"
                problems.append(f"unknown option {key!r} (known: {known})")
                continue
            try:
                coerced = _coerce_option(key, kind, value)
                self.validate_option(key, coerced)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            setattr(self, key, coerced)
        return problems

    def validate_option(self, key: str, value: object) -> None:
        """Hook-specific value check; raise :class:`ValueError` to reject it."""


def _coerce_option(key: str, kind: OptionKind, value: object) -> object:
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string, got {type(value).__name__}")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false, got {value!r}")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer, got {value!r}")
        return value
    return _coerce_events(key, value)


def _coerce_events(key: str, value: object) -> list[str]:
    """Normalize ``events`` to a list and refuse names that are not events."""
    if isinstance(value, str):
        # A single name is accepted *and normalised*; keeping the string would
        # silently turn the membership test into a substring test.
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        raise ValueError(f"{key} must be a list of events, got {type(value).__name__}")
    unknown = sorted({item for item in items if item not in EVENTS})
    if unknown:
        raise ValueError(
            f"{key} names unknown event(s) {', '.join(unknown)}; known: {', '.join(EVENTS)}"
        )
    return items


def apply_hook_options(hook: object, options: Mapping[str, object]) -> list[str]:
    """Apply configured options to ``hook``, returning what could not be applied.

    A hook that declares ``OPTIONS`` (i.e. the builtin ones) is validated. A
    third-party hook without a declaration still gets its unknown keys reported
    instead of dropped; everything it does define is assigned as before.
    """
    configure = getattr(hook, "configure", None)
    if callable(configure):
        return list(configure(options))
    problems = [f"unknown option {key!r}" for key in options if not hasattr(hook, key)]
    for key, value in options.items():
        if hasattr(hook, key):
            setattr(hook, key, value)
    return problems


class HookTasks:
    """Background work owned by one hook, with its failures made visible."""

    def __init__(self, name: str) -> None:
        self.name = name
        #: How many background jobs have failed so far.
        self.failed = 0
        self._pending: set[asyncio.Task[object]] = set()

    def _start(self, factory: Callable[[], Coroutine[Any, Any, object]]) -> bool:
        """Schedule ``factory()`` unless there is no running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only in sync contexts
            return False
        task = loop.create_task(factory())
        self._pending.add(task)
        task.add_done_callback(self._finished)
        return True

    def spawn(self, factory: Callable[[], Coroutine[Any, Any, object]]) -> bool:
        """Run the coroutine that ``factory`` builds; ``False`` without a loop."""
        return self._start(factory)

    def spawn_thread(self, func: Callable[..., object], *args: Any) -> bool:
        """Run ``func(*args)`` in a worker thread; ``False`` without a loop."""
        return self._start(lambda: asyncio.to_thread(func, *args))

    def record(self) -> None:
        """Count a failure this hook detected and already reported.

        A hook that catches its own error still failed to do its job; saying so
        keeps ``strict_hooks`` honest without printing the reason twice.
        """
        self.failed += 1

    def _finished(self, task: asyncio.Task[object]) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.failed += 1
            console.warn(f"{self.name}: {redact_text(str(error))}")

    async def aclose(self) -> int:
        """Wait for in-flight work and return how many jobs failed."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        return self.failed


class EventBus:
    """Dispatches events to Hook plugins and ad-hoc handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._hooks: list[Hook] = []
        #: How many hook failures this run has seen (sync handlers plus
        #: background work reported by ``aclose``).
        self.failures = 0

    def register_hook(self, hook: Hook) -> None:
        self._hooks.append(hook)
        for event in EVENTS:

            def handler(
                payload: dict[str, object], _hook: Hook = hook, _event: str = event
            ) -> None:
                _hook.on_event(_event, payload)

            self._handlers[event].append(handler)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def emit(self, event: str, payload: dict[str, object]) -> None:
        if event == "on_finish":
            # The final event has to say whether post-processing worked; a hook
            # that raised during the run was reported, and this is the machine
            # readable form of the same fact.
            payload.setdefault("hooks_failed", self.failures)
        for handler in self._handlers.get(event, []):
            try:
                handler(payload)
            except Exception as exc:  # noqa: BLE001 - a hook must never break a run
                self.failures += 1
                console.warn(f"hook error on {event}: {redact_text(str(exc))}")

    async def aclose(self) -> None:
        """Let hooks that do async work (e.g. webhooks) flush before exit."""
        for hook in self._hooks:
            close = getattr(hook, "aclose", None)
            if close is None:
                continue
            try:
                reported = await close()
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                console.warn(f"hook shutdown error: {redact_text(str(exc))}")
                continue
            if isinstance(reported, int):
                self.failures += max(0, reported)
