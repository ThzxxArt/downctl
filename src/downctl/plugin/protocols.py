"""Plugin contracts for downctl.

Every plugin is any object satisfying one of the ``Protocol`` classes below.
Protocols are ``runtime_checkable`` so the registry can validate third-party
plugins without forcing them to inherit from a base class. No plugin needs to
import downctl to be discovered structurally, though using these Protocols for
type hints is encouraged.

The plugin contract is versioned by :data:`API_VERSION`. Plugins must expose a
``api_version`` attribute; the registry rejects incompatible ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from downctl import API_VERSION as _API_VERSION

#: The plugin API version this build supports.
API_VERSION = _API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask


@runtime_checkable
class Plugin(Protocol):
    """Common metadata every plugin must expose."""

    #: Unique plugin name (used by ``--source`` / ``--reporter`` etc.).
    name: str
    #: Plugin API version the plugin was built against, e.g. ``"1"``.
    api_version: str
    #: Higher wins when several plugins can handle the same input.
    priority: int


@runtime_checkable
class Source(Plugin, Protocol):
    """Turns user input into concrete download tasks."""

    def can_handle(self, target: str) -> bool:
        """Return whether this source understands ``target``."""
        ...

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        """Expand ``target`` into one or more :class:`DownloadTask`."""
        ...


@runtime_checkable
class Response(Protocol):
    """A streaming HTTP response handed back by a :class:`Transport`."""

    status: int
    headers: Mapping[str, str]

    @property
    def content_length(self) -> int | None:
        """Declared body length, or ``None`` when unknown."""
        ...

    def iter_chunks(self, size: int = 65536) -> AsyncIterator[bytes]:
        """Yield body bytes in chunks of at most ``size``."""
        ...


@runtime_checkable
class Transport(Plugin, Protocol):
    """Fetches bytes. The engine drives this in the piece loop.

    ``request`` returns an async context manager yielding a
    :class:`Response` (or raises :class:`~downctl.core.errors.TransportError`
    on transient failures). Transports must map their native errors onto the
    downctl exception hierarchy so the core never depends on a specific HTTP
    library.
    """

    def supports_range(self) -> bool:
        """Whether ranged (resumable) requests are supported."""
        ...

    def request(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> AbstractAsyncContextManager[Response]:
        """Open ``url`` (optionally a byte range) and yield a response."""
        ...


@runtime_checkable
class Verifier(Plugin, Protocol):
    """Verifies a downloaded file against an expected digest."""

    #: Algorithm name, e.g. ``"sha256"``.
    algo: str

    async def verify(self, path: str, expected_hex: str) -> bool:
        """Return ``True`` if ``path`` matches ``expected_hex``."""
        ...


@runtime_checkable
class Sink(Plugin, Protocol):
    """Where bytes land. Enables non-filesystem destinations."""

    async def open(self, task: DownloadTask, ctx: RuntimeContext) -> None: ...

    async def write(self, offset: int, data: bytes) -> None: ...

    async def close(self, *, success: bool) -> None: ...


@runtime_checkable
class Reporter(Protocol):
    """Progress and lifecycle presentation.

    Optional extensions (not part of the structural contract, so they are
    feature-detected with ``getattr``):

    - ``run_started(total_tasks, total_bytes, jobs)`` called once before a run.
    - ``run_resumed(done_bytes)`` to seed an overall bar with already-complete
      bytes from a resumed run.
    - ``task_resumed(task, done_bytes)`` to jump a per-file bar to its resume
      point.
    - ``run_finished(succeeded, failed)`` called once after a run.
    """

    def task_started(self, task: DownloadTask) -> None: ...

    def task_progress(self, task: DownloadTask, delta: int) -> None: ...

    def task_finished(self, task: DownloadTask, ok: bool) -> None: ...


@runtime_checkable
class Router(Plugin, Protocol):
    """Rewrites URLs before fetching (mirrors, CDN failover, ...)."""

    def rewrite(self, url: str) -> str:
        """Return the URL to actually fetch for ``url``."""
        ...


@runtime_checkable
class Hook(Plugin, Protocol):
    """Observes lifecycle events and reacts (logging, notify, post-process)."""

    def on_event(self, event: str, payload: dict[str, object]) -> None: ...
