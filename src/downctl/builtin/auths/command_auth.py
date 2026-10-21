"""The ``command`` auth provider: ask a local program for a token.

Options (``[auth."<host>"]``):

- ``command``: shell command to run (required)
- ``timeout``: seconds before it is killed (default ``10``; must be > 0)
- ``header``:  header to fill (default ``Authorization``)
- ``prefix``:  value prefix (default ``Bearer ``; may be empty)
- ``ttl``:     consumed by the resolver (renewal interval), not by this plugin

The command's standard output must be exactly one non-empty line: that line is
the token. More than one line is refused rather than guessed, because printing
a value plus a progress message is a common script bug and picking either line
would silently send the wrong string. Standard error is never read into the
message; it may contain whatever the script logged, including the token.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING

from downctl.core.credentials import AuthError, AuthMaterial
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Every option this provider understands. ``ttl`` is validated by the
#: resolver and deliberately ignored here.
_KNOWN: frozenset[str] = frozenset({"command", "timeout", "header", "prefix", "ttl"})


class CommandAuth:
    """Produce an authorization header from a command's single output line."""

    name = "command"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, host: str) -> bool:
        return True

    async def material(
        self,
        host: str,
        ctx: RuntimeContext,
        options: Mapping[str, object],
    ) -> AuthMaterial | None:
        unknown = sorted(set(options) - _KNOWN)
        if unknown:
            raise AuthError(
                f"command auth: unknown option {unknown[0]!r} (known: {', '.join(sorted(_KNOWN))})"
            )
        command = _command(options)
        timeout = _timeout(options.get("timeout", 10))
        header = _string(options, "header", "Authorization", allow_empty=False)
        prefix = _string(options, "prefix", "Bearer ", allow_empty=True)

        value = await _run(command, timeout)
        return AuthMaterial(
            headers={header: prefix + value},
            # Never the command line itself: it may contain the token inline
            # (``echo ghp_...``), and ``source`` is shown by ``auth check``.
            source="command",
        )


async def _run(command: str, limit: float) -> str:
    """Run ``command`` and return its single output line (stripped)."""
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            # The token goes to stdout; stderr is drained so a chatty script
            # cannot fill the pipe and deadlock, but is never interpreted.
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A command that unexpectedly prompts must fail, not hang.
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        # ``exc`` describes the shell/exec failure; it cannot contain the
        # token because nothing ran yet.
        raise AuthError(f"command auth: could not start the command: {exc}") from exc

    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), limit)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AuthError(f"command auth: command timed out after {_seconds(limit)}s") from None

    # Exit status is checked before the output: a failed command's output is
    # not a token, and "exited 3" says more than "produced no output".
    if process.returncode != 0:
        raise AuthError(f"command auth: command exited {process.returncode}")

    text = stdout.decode("utf-8", errors="replace").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise AuthError("command auth: command produced no output")
    if len(lines) > 1:
        raise AuthError(
            "command auth: command produced more than one line; "
            "the command must print only the token"
        )
    return lines[0].strip()


def _seconds(value: float) -> str:
    """Format a timeout without a trailing ``.0`` (``10s``, not ``10.0s``)."""
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _command(options: Mapping[str, object]) -> str:
    if "command" not in options:
        raise AuthError("command auth: missing required option 'command'")
    raw = options["command"]
    if not isinstance(raw, str):
        raise AuthError(
            f"command auth: option 'command' must be a string, got {type(raw).__name__}"
        )
    if not raw.strip():
        raise AuthError("command auth: option 'command' must not be empty")
    return raw


def _timeout(raw: object) -> float:
    # ``bool`` is an ``int`` subclass; ``timeout = true`` is a typo, not one
    # second.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AuthError(
            f"command auth: option 'timeout' must be a number, got {type(raw).__name__}"
        )
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise AuthError("command auth: option 'timeout' must be a positive number")
    return value


def _string(
    options: Mapping[str, object],
    key: str,
    default: str,
    *,
    allow_empty: bool,
) -> str:
    raw = options.get(key, default)
    if not isinstance(raw, str):
        raise AuthError(f"command auth: option {key!r} must be a string, got {type(raw).__name__}")
    if not allow_empty and not raw.strip():
        raise AuthError(f"command auth: option {key!r} must not be empty")
    return raw


__all__ = ["CommandAuth"]
