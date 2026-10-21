"""The notify hook plugin: send a run summary to a URL or a command.

Options (``[plugins.notify]``):

- ``url``:           POST a JSON notification to this endpoint
- ``topic``:         ntfy topic; when set the payload is ntfy-style
- ``command``:       run this shell command instead of POSTing
- ``events``:        events that trigger a notification (default ``["on_finish"]``)
- ``only_failures``: skip a clean ``on_finish`` (default ``false``)
- ``title``:         notification title (default ``downctl``)
- ``timeout``:       command timeout in seconds (default ``10.0``)

Exactly one of ``url`` and ``command`` must be configured. Delivery happens on
a background task (or a subprocess), so a slow notifier never blocks a
download. A command that outlives ``timeout`` has its process group killed and
is reported as a hook failure. Failures are reported with ``always=True`` and
counted on the hook's :class:`HookTasks`, which is what ``core.strict_hooks``
inspects — a broken notification path must not look like a successful run.

Without ``topic`` the URL receives the generic webhook shape
``{"title", "message", "url"}``. With ``topic`` it receives
``{"topic", "message", "title"}``, which is what ntfy expects at its base URL.

The message never carries credentials: the body is passed through
:func:`downctl.urls.redact_text` and a URL backend's own address is only
printed through :func:`downctl.urls.display_url`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Mapping

from downctl import console
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION
from downctl.urls import display_url, redact_text

#: Request timeout for the URL backend, in seconds.
_HTTP_TIMEOUT = 10


class NotifyHook(ConfigurableHook):
    """POST or shell out a one-line summary when a lifecycle event fires."""

    name = "notify"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {
        "url": "str",
        "topic": "str",
        "command": "str",
        "events": "events",
        "only_failures": "bool",
        "title": "str",
        "timeout": "float",
    }

    def __init__(
        self,
        url: str = "",
        command: str = "",
        events: list[str] | None = None,
        only_failures: bool = False,
        title: str = "downctl",
        topic: str = "",
        timeout: float = 10.0,
    ) -> None:
        if url and command:
            raise ValueError("set exactly one of url or command, not both")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.url = url
        self.topic = topic
        self.command = command
        self.events = events if events is not None else ["on_finish"]
        self.only_failures = only_failures
        self.title = title
        self.timeout = float(timeout)
        self._tasks = HookTasks(self.name)
        self._invalid_reported = False

    def validate_option(self, key: str, value: object) -> None:
        if key == "events" and not value:
            raise ValueError("events must name at least one event")
        if key == "url" and value and self.command:
            raise ValueError("set exactly one of url or command, not both")
        if key == "command" and value and self.url:
            raise ValueError("set exactly one of url or command, not both")
        if key == "timeout" and isinstance(value, (int, float)) and value <= 0:
            raise ValueError("timeout must be a positive number of seconds")

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event not in self.events:
            return
        if bool(self.url) == bool(self.command):
            self._warn_invalid_backend()
            return
        if self.only_failures and event == "on_finish" and _as_int(payload.get("failed")) == 0:
            return

        body = _body(payload)
        if self.url:
            if not self._tasks.spawn(lambda: self._post(body, payload)):
                asyncio.run(self._post(body, payload))
        elif not self._tasks.spawn(lambda: self._run_command(event, payload)):
            asyncio.run(self._run_command(event, payload))

    async def aclose(self) -> int:
        """Wait for in-flight deliveries and return failures not yet reported."""
        return await self._tasks.aclose()

    def _warn_invalid_backend(self) -> None:
        if self._invalid_reported:
            return
        self._invalid_reported = True
        console.warn(
            "notify: set exactly one of url or command in [plugins.notify]",
            always=True,
        )
        self._tasks.record()

    async def _post(self, body: str, payload: Mapping[str, object]) -> None:
        import aiohttp

        if self.topic:
            # ntfy's JSON publish shape: the topic is selected by this field
            # when POSTing to the server's base URL.
            message: dict[str, object] = {
                "topic": self.topic,
                "message": body,
                "title": self.title,
            }
        else:
            message = {
                "title": self.title,
                "message": body,
                "url": display_url(self.url),
            }
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self.url, json=message) as response,
            ):
                if not 200 <= response.status < 300:
                    console.warn(
                        f"notify: POST to {display_url(self.url)} returned HTTP {response.status}",
                        always=True,
                    )
                    self._tasks.record()
        except Exception as exc:  # noqa: BLE001 - a notification failure is not fatal
            console.warn(
                f"notify: POST to {display_url(self.url)} failed: {_failure_text(exc)}",
                always=True,
            )
            self._tasks.record()

    async def _run_command(self, event: str, payload: Mapping[str, object]) -> None:
        env = dict(os.environ)
        env.update(
            {
                "DOWNCTL_EVENT": event,
                "DOWNCTL_SUCCEEDED": str(_as_int(payload.get("succeeded"))),
                "DOWNCTL_FAILED": str(_as_int(payload.get("failed"))),
                "DOWNCTL_BYTES": str(_as_int(payload.get("bytes"))),
                "DOWNCTL_ELAPSED": _number(payload.get("elapsed")),
            }
        )
        targets = _target_names(payload.get("targets")) or _target_names(payload.get("task"))
        if targets:
            env["DOWNCTL_TARGET"] = targets[0]
        try:
            process = await asyncio.create_subprocess_shell(  # noqa: S604 - user-configured
                self.command,
                env=env,
                # A hook must not corrupt a machine-readable JSON stream; this
                # mirrors what the exec hook does with its child's stdout.
                stdout=sys.stderr if console.is_machine_readable() else None,
                # A shell is the process-group leader, so a timeout can kill the
                # whole pipeline rather than just the shell that spawned it.
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            console.warn(f"notify: command failed: {_failure_text(exc)}", always=True)
            self._tasks.record()
            return

        try:
            returncode = await asyncio.wait_for(
                asyncio.shield(process.wait()), timeout=self.timeout
            )
        except TimeoutError:
            await _terminate_process_group(process)
            console.warn(
                f"notify: command timed out after {self.timeout:g}s and was terminated",
                always=True,
            )
            self._tasks.record()
            return
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            raise
        if returncode != 0:
            console.warn(f"notify: command exited with {returncode}", always=True)
            self._tasks.record()


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill a timed-out notifier and reap it, including its children when possible."""
    if process.returncode is not None:
        return
    if hasattr(os, "killpg"):
        # Missing group / permission denied: the direct kill below is the
        # documented fallback, not a reason to leave the child running.
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(OSError):
        await process.wait()


def _failure_text(exc: BaseException) -> str:
    """A non-empty failure description for a warning (``TimeoutError()`` is ``""``)."""
    text = redact_text(str(exc)).strip()
    return text or type(exc).__name__


def _body(payload: Mapping[str, object]) -> str:
    """Build the one-line notification text, with embedded URLs redacted."""
    parts = [
        f"{_as_int(payload.get('succeeded'))} succeeded",
        f"{_as_int(payload.get('failed'))} failed",
        f"{_as_int(payload.get('bytes'))} bytes",
        f"{_number(payload.get('elapsed'))}s elapsed",
    ]
    names = _target_names(payload.get("targets")) or _target_names(payload.get("task"))
    if names:
        parts.append("targets: " + ", ".join(names))
    error = payload.get("error")
    if error:
        parts.append(f"error: {error}")
    return _one_line(redact_text("; ".join(parts)))


def _target_names(value: object) -> list[str]:
    """Read display names from ``targets`` (objects or plain strings)."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    names: list[str] = []
    for item in items:
        name = str(getattr(item, "target", item)).strip()
        if name:
            names.append(name)
    return names


def _one_line(text: str) -> str:
    """Collapse newlines/tabs so a notification body stays one line."""
    return " ".join(text.split())


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _number(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "0"
