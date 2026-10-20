"""Include/exclude filtering, shared by every source plugin.

Each source that expands a tree answered the same two questions with its own
copy of the same two lines: is this file included, and is it excluded. Written
once so the answer cannot drift between sources — and so the rule is stated in
one place:

- ``--include`` is a *filter*: empty means everything, otherwise the path must
  match at least one pattern.
- ``--exclude`` always wins over ``--include``.
- Patterns match the **source-relative** path (``sub/model.bin``), so ``*.bin``
  covers the whole tree and ``sub/*.bin`` only that directory.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


def matches(relative: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """Whether ``relative`` passes ``--include``/``--exclude``."""
    if include and not any(fnmatch.fnmatchcase(relative, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclude)


def filters(ctx: RuntimeContext) -> tuple[list[str], list[str]]:
    """The ``--include`` and ``--exclude`` lists of this run."""
    return (
        list(ctx.params.get("include") or []),
        list(ctx.params.get("exclude") or []),
    )
