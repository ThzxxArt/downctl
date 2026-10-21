"""The ``env`` auth provider: read a token from an environment variable.

Options (``[auth."<host>"]``):

- ``env``:    one variable name, or a list tried in order (required)
- ``header``: header to fill (default ``Authorization``)
- ``prefix``: value prefix (default ``Bearer ``; may be empty)

The first variable that is set to a non-empty value wins. "Unset" is a
configuration error, not "no credentials": the user asked for this host to be
authenticated from an environment variable, so silently downloading without
one is the failure mode this provider exists to prevent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from downctl.core.credentials import AuthError, AuthMaterial
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Every option this provider understands. Anything else is a typo, not a
#: feature request, and silently ignoring it is how a token goes missing.
_KNOWN: frozenset[str] = frozenset({"env", "header", "prefix"})


class EnvAuth:
    """Produce an authorization header from an environment variable."""

    name = "env"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, host: str) -> bool:
        # The explicit ``[auth."<host>"]`` rule is the decision; by the time
        # this provider is asked, the host already matched a rule.
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
                f"env auth: unknown option {unknown[0]!r} (known: {', '.join(sorted(_KNOWN))})"
            )
        names = _names(options.get("env"))
        header = _string(options, "header", "Authorization", allow_empty=False)
        prefix = _string(options, "prefix", "Bearer ", allow_empty=True)

        for name in names:
            value = os.environ.get(name, "")
            if value:
                return AuthMaterial(
                    headers={header: prefix + value},
                    source=f"env {name}",
                )
        joined = ", ".join(names)
        if len(names) == 1:
            raise AuthError(f"env {joined} is unset")
        raise AuthError(f"env {joined} are all unset")


def _names(raw: object) -> list[str]:
    """Validate the ``env`` option: a name or a non-empty list of names."""
    if raw is None:
        raise AuthError("env auth: missing required option 'env'")
    if isinstance(raw, str):
        names = [raw]
    elif isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        names = list(raw)
    else:
        raise AuthError(
            "env auth: option 'env' must be a string or a list of strings, "
            f"got {type(raw).__name__}"
        )
    if not names or any(not name.strip() for name in names):
        raise AuthError("env auth: option 'env' must name at least one environment variable")
    return names


def _string(
    options: Mapping[str, object],
    key: str,
    default: str,
    *,
    allow_empty: bool,
) -> str:
    raw = options.get(key, default)
    if not isinstance(raw, str):
        raise AuthError(f"env auth: option {key!r} must be a string, got {type(raw).__name__}")
    if not allow_empty and not raw.strip():
        raise AuthError(f"env auth: option {key!r} must not be empty")
    return raw


__all__ = ["EnvAuth"]
