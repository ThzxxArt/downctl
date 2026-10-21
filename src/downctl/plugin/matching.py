"""Include/exclude filtering, shared by every source plugin.

Each source that expands a tree answered the same two questions with its own
copy of the same two lines: is this file included, and is it excluded. Written
once so the answer cannot drift between sources — and so the rule is stated in
one place:

- ``--include`` is a *filter*: empty means everything, otherwise the path must
  match at least one pattern.
- ``--exclude`` always wins over ``--include``.
- Patterns use **path** semantics: ``*`` and ``?`` never cross ``/``, ``**``
  does, and a pattern with no ``/`` is matched against the file name (so
  ``*.bin`` still covers the whole tree). ``sub/*.bin`` therefore matches
  ``sub/a.bin`` but not ``sub/x/a.bin`` — ``fnmatch`` used to let ``*`` cross
  the separator, so a pattern that read "one level" matched every level.

"Source-relative" is whatever the source enumerates: a repository tree gives
directories, while a GitHub release or an SFTP directory listing gives bare file
names, and a pattern containing ``/`` then simply never matches. The same two
lines are used either way; the caller owns the shape of the string.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a glob into a path-aware regex (``**`` crosses ``/``)."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "[":
            end = pattern.find("]", index + 2)
            if end != -1:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                # ``fnmatch`` supported bracket expressions before the path
                # semantics rewrite; keeping them means ``a[0-9].txt`` still
                # means what a user expects from a glob.
                parts.append("[" + body.replace("\\", "\\\\") + "]")
                index = end + 1
                continue
        if character == "*":
            if pattern[index : index + 2] == "**":
                if pattern[index + 2 : index + 3] == "/":
                    # ``**/`` matches zero or more directories, like glob(3).
                    parts.append("(?:.*/)?")
                    index += 3
                    continue
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if character == "?":
            parts.append("[^/]")
            index += 1
            continue
        parts.append(re.escape(character))
        index += 1
    return re.compile("".join(parts) + r"\Z")


def _matches_one(relative: str, pattern: str) -> bool:
    candidate = relative.replace("\\", "/")
    if "/" not in pattern:
        candidate = candidate.rsplit("/", 1)[-1]
    return _compile(pattern).match(candidate) is not None


def matches(relative: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """Whether ``relative`` passes ``--include``/``--exclude``."""
    if include and not any(_matches_one(relative, pattern) for pattern in include):
        return False
    return not any(_matches_one(relative, pattern) for pattern in exclude)


def filters(ctx: RuntimeContext) -> tuple[list[str], list[str]]:
    """The ``--include`` and ``--exclude`` lists of this run."""
    return (
        list(ctx.params.get("include") or []),
        list(ctx.params.get("exclude") or []),
    )
