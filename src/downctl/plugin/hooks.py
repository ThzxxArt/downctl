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
OptionKind = Literal["str", "int", "bool", "float", "events", "list", "choice"]
#: A declaration is a kind name, or ``("choice", ("a", "b"))`` for an enum.
OptionSpec = str | tuple[str, tuple[str, ...]]

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

    #: Option name -> kind. Kinds: ``str``, ``int``, ``bool``, ``float``,
    #: ``events``, ``list``; ``choice`` additionally names its allowed values
    #: as ``("choice", ("a", "b"))``.
    OPTIONS: ClassVar[Mapping[str, OptionSpec]] = {}

    def configure(self, options: Mapping[str, object]) -> list[str]:
        """Apply ``options``; return the problems found (empty when clean)."""
        problems: list[str] = []
        for key, value in options.items():
            spec = self.OPTIONS.get(key)
            if spec is None:
                known = ", ".join(sorted(self.OPTIONS)) or "none"
                problems.append(f"unknown option {key!r} (known: {known})")
                continue
            try:
                coerced = _coerce_option(key, spec, value)
                self.validate_option(key, coerced)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            setattr(self, key, coerced)
        return problems

    def validate_option(self, key: str, value: object) -> None:
        """Hook-specific value check; raise :class:`ValueError` to reject it."""


def _coerce_option(key: str, spec: OptionSpec, value: object) -> object:
    kind: str
    allowed: tuple[str, ...] = ()
    if isinstance(spec, tuple):
        kind, allowed = spec
    else:
        kind = spec
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
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number, got {value!r}")
        return float(value)
    if kind == "list":
        if isinstance(value, str):
            return [value]
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a list of strings, got {value!r}")
        return list(value)
    if kind == "choice":
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
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
    instead of dropped — and its *typed* keys checked against the attribute it
    already has, because ``bool("false") is True`` applied to a third-party
    hook too: a value that only looks off changed the meaning of the option.
    """
    configure = getattr(hook, "configure", None)
    if callable(configure):
        try:
            result = configure(options)
        except Exception as exc:  # noqa: BLE001 - a hook must not abort the run
            return [f"configure() failed: {redact_text(str(exc)) or type(exc).__name__}"]
        if result is None:
            # The protocol never required a return value; a third-party
            # ``configure`` that mutates ``self`` and returns ``None`` used to
            # crash the run with "NoneType is not iterable" before any
            # download.
            return []
        if isinstance(result, (list, tuple)):
            return [str(problem) for problem in result]
        return [f"configure() returned {type(result).__name__}, expected a list of problems"]
    problems: list[str] = []
    for key, value in options.items():
        if not hasattr(hook, key):
            problems.append(f"unknown option {key!r}")
            continue
        problem = _type_problem(getattr(hook, key), value)
        if problem:
            problems.append(f"{key} {problem}")
            continue
        setattr(hook, key, value)
    return problems


def _type_problem(current: object, value: object) -> str:
    """Why ``value`` cannot replace ``current``, or ``""`` when it can."""
    if isinstance(current, bool):
        return "" if isinstance(value, bool) else "must be true or false"
    if isinstance(current, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be an integer"
        return ""
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a number"
        return ""
    if isinstance(current, str):
        return "" if isinstance(value, str) else "must be a string"
    if isinstance(current, (list, tuple)):
        return "" if isinstance(value, (list, tuple)) else "must be a list"
    return ""


class HookTasks:
    """Background work owned by one hook, with its failures made visible."""

    def __init__(self, name: str) -> None:
        self.name = name
        #: How many background jobs have failed so far.
        self.failed = 0
        #: How many of those a previous ``aclose`` already reported. The count
        #: handed back is a delta, so draining twice cannot double-count.
        self._reported = 0
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
            console.warn(f"{self.name}: {redact_text(str(error))}", always=True)

    async def aclose(self) -> int:
        """Wait for in-flight work and return failures not yet reported.

        The value is a delta, not a total: the run drains hooks before it
        announces its end and the CLI closes them on the way out, so a
        cumulative count would be added to the run's total twice.
        """
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        fresh = self.failed - self._reported
        self._reported = self.failed
        return fresh


class EventBus:
    """Dispatches events to Hook plugins and ad-hoc handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._hooks: list[Hook] = []
        #: How many hook failures this run has seen (sync handlers plus
        #: background work reported by ``aclose``).
        self.failures = 0
        self._closed = False

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
        # Every consumer of an event (the logfile hook, a webhook, an exec
        # child) sees its payload; masking here is the one place that reaches
        # all of them. An exception's text often embeds the request URL, and a
        # presigned query must not reach a log file either.
        error = payload.get("error")
        if isinstance(error, str):
            payload["error"] = redact_text(error)
        if event == "on_finish":
            # The final event has to say whether post-processing worked; a hook
            # that raised during the run was reported, and this is the machine
            # readable form of the same fact. ``finalize`` is the owner of the
            # number (it drains first); this default only serves callers that
            # emit the event themselves.
            payload.setdefault("hooks_failed", self.failures)
        for handler in self._handlers.get(event, []):
            try:
                handler(payload)
            except Exception as exc:  # noqa: BLE001 - a hook must never break a run
                self.failures += 1
                # ``always``: 0.1.5 routed this through the quiet-able channel,
                # so ``-q`` made every hook failure invisible while the docs
                # promised "Hook failures are never silently swallowed".
                console.warn(f"hook error on {event}: {redact_text(str(exc))}", always=True)

    async def finalize(self) -> int:
        """Drain background work and return the run's total hook failures.

        ``on_finish`` used to count failures *before* the hooks that run on it,
        so a run whose extraction failed in a worker thread reported
        ``hooks_failed=0``. The owner of the number is the bus: it drains the
        work it knows about, then hands the total to the caller, which puts it
        in the payload. A failure that happens *during* ``on_finish`` cannot be
        part of the event that triggers it; it is reported by ``aclose`` and
        counted by ``strict_hooks``.
        """
        await self.drain()
        return self.failures

    async def drain(self) -> None:
        """Wait for background hook work without closing any hook.

        ``on_finish`` carries a failure count, and a count taken before the
        work finished always reads zero: the checksum/exec/webhook hooks all
        fail in a worker thread, so their failures arrived after the event
        that was supposed to report them. Draining first makes it final.
        """
        for hook in self._hooks:
            tasks = getattr(hook, "_tasks", None)
            if isinstance(tasks, HookTasks):
                self.failures += await tasks.aclose()

    async def aclose(self) -> None:
        """Let hooks that do async work (e.g. webhooks) flush before exit.

        Idempotent: the plan closes the bus before evaluating
        ``core.strict_hooks`` (worker-thread failures only become visible
        during ``aclose``), and the command's ``finally`` closes it again.
        """
        if self._closed:
            return
        self._closed = True
        for hook in self._hooks:
            close = getattr(hook, "aclose", None)
            if close is None:
                continue
            try:
                reported = await close()
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                console.warn(f"hook shutdown error: {redact_text(str(exc))}", always=True)
                continue
            if isinstance(reported, int):
                self.failures += max(0, reported)
