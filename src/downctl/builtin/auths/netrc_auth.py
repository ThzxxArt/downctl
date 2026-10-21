"""The ``netrc`` auth provider: read HTTP Basic credentials from a netrc file.

Options (``[auth."<host>"]``):

- ``path``: netrc file (default ``~/.netrc``, expanded with
  :func:`os.path.expanduser`)

A missing file or a file without a ``machine`` entry means "no material" and
returns ``None``; a file that exists but cannot be parsed is an error, because
silently falling back to an anonymous request hides a broken configuration.

When the file on POSIX is group- or world-accessible, a warning is printed that
names the file and the mode. The credentials themselves are never printed.
"""

from __future__ import annotations

import netrc
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from downctl import console
from downctl.core.credentials import AuthError, AuthMaterial
from downctl.plugin.protocols import API_VERSION
from downctl.urls import redact_text

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Every option this provider understands.
_KNOWN: frozenset[str] = frozenset({"path"})

_DEFAULT_PATH = "~/.netrc"

#: Mode bits that let someone other than the owner read the file.
_OTHERS_READABLE = 0o077


class NetrcAuth:
    """Produce HTTP Basic credentials from a ``~/.netrc`` entry."""

    name = "netrc"
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
                f"netrc auth: unknown option {unknown[0]!r} (known: {', '.join(sorted(_KNOWN))})"
            )
        path = _path(options)
        _warn_if_readable(path)
        authenticators = _authenticators(path, host)
        if authenticators is None:
            return None
        login, _account, password = authenticators
        return AuthMaterial(basic=(login, password), source=f"netrc {path}")


def _path(options: Mapping[str, object]) -> str:
    raw = options.get("path", _DEFAULT_PATH)
    if not isinstance(raw, str):
        raise AuthError(f"netrc auth: option 'path' must be a string, got {type(raw).__name__}")
    if not raw.strip():
        raise AuthError("netrc auth: option 'path' must not be empty")
    return os.path.expanduser(raw)


def _warn_if_readable(path: str) -> None:
    """Warn when netrc is readable by group/others (POSIX only).

    ``netrc.netrc()`` enforces this only for its *default* path; an explicit
    ``path`` is not checked, so the warning is the only signal the user gets.
    """
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        # Missing file is not a permission problem; the read below decides
        # whether that means "no material" or an error.
        return
    if mode & _OTHERS_READABLE:
        console.warn(
            f"netrc {path} is readable by others (mode {mode & 0o777:03o}); run `chmod 600 {path}`",
            always=True,
        )


def _authenticators(path: str, host: str) -> tuple[str, str | None, str] | None:
    """Parse ``path`` and return the entry for ``host``, or ``None``.

    A ``NetrcParseError``'s own message quotes the offending token, which is
    exactly the credential a malformed line failed around (``bad follower token
    's3cret'``). It is therefore never copied into the error: the file, the line
    and the class of problem are enough to fix the file without printing the
    secret that caused it.
    """
    try:
        parsed = netrc.netrc(path)
    except FileNotFoundError:
        return None
    except netrc.NetrcParseError as exc:
        raise AuthError(
            f"netrc auth: cannot parse {path} (line {exc.lineno}): invalid netrc syntax"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AuthError(f"netrc auth: cannot read {path}: {redact_text(str(exc))}") from exc
    return parsed.authenticators(host)


__all__ = ["NetrcAuth"]
