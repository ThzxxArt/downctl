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
    from downctl.core.credentials import AuthMaterial
    from downctl.core.job import Job
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
    """Turns user input into concrete download tasks.

    Optional capability (feature-detected, not part of the structural check):
    ``supports_filters`` defaults to ``True``; set it to ``False`` for a source
    with no listing to filter (the plain-URL source). ``False`` means *not
    supported*: ``capabilities()``/``plugin info`` report the flag only when it
    is ``True``, and the app warns when ``--include/--exclude`` are given for a
    source that opted out instead of silently ignoring them.
    """

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

    Optional extensions (not part of the structural contract, so they are
    feature-detected with ``getattr`` and no plugin has to implement them):

    - ``sends_content_range = True``: a ranged answer (``206``) always carries
      a ``Content-Range`` describing the returned window, so the core asserts
      it matches the request instead of trusting the body's position. Set it to
      ``True`` on any HTTP-based transport; leave it unset (or ``False``) on
      transports whose ranged answers have no such header. Only ``True`` is a
      promise and only ``True`` is reported as a capability.
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
class Reporter(Plugin, Protocol):
    """Progress and lifecycle presentation.

    Optional extensions (not part of the structural contract, so they are
    feature-detected with ``getattr``):

    - ``machine_readable = True``: stdout belongs to this reporter alone; the
      CLI silences human output (errors still go to stderr). Only ``True`` is a
      promise and only ``True`` is reported as a capability; an unset or
      ``False`` flag means human output.
    - ``failure(target, error, url, reason)`` for a target that failed before
      the engine ran (resolve/probe).
    - ``run_started(total_tasks, total_bytes, jobs)`` called once before a run.
    - ``run_resumed(done_bytes)`` to seed an overall bar with already-complete
      bytes from a resumed run.
    - ``task_resumed(task, done_bytes)`` to jump a per-file bar to its resume
      point.
    - ``run_finished(succeeded, failed, *, job="", hooks_failed=0)`` called
      once after a run.
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
    """Observes lifecycle events and reacts (logging, notify, post-process).

    Optional extensions (feature-detected by the event bus):

    - ``OPTIONS`` / ``configure(mapping)``: declared options are type-checked;
      a hook without them still has its typed attributes checked.
    - ``aclose()``: await background work; the return value is the number of
      failures not yet reported (``strict_hooks`` reads it).
    - ``_tasks``: a :class:`~downctl.plugin.hooks.HookTasks` owned by the hook;
      its drained failure count is what ``aclose`` returns.
    """

    def on_event(self, event: str, payload: dict[str, object]) -> None: ...


# --------------------------------------------------------------------------- #
# 0.1.6 categories
# --------------------------------------------------------------------------- #
@runtime_checkable
class JobSource(Plugin, Protocol):
    """Turns a job file into the jobs it describes.

    One question only: "what runs are in this file?". A job may carry its own
    options, so this cannot be a :class:`Source`: ``Source.resolve`` returns
    files, while ``dir``/``include``/``force`` describe a *run*.
    """

    #: File extensions this source understands (lower-case, no dot).
    formats: tuple[str, ...]

    def can_handle(self, path: str) -> bool:
        """Return whether this plugin understands ``path``."""
        ...

    async def load(self, path: str, ctx: RuntimeContext) -> list[Job]:
        """Parse ``path`` into jobs. Raise on anything invalid."""
        ...


@runtime_checkable
class AuthProvider(Plugin, Protocol):
    """Produces the material one host is entitled to.

    Returns *material*, never a decision: which hop of a redirect may carry it
    stays owned by :func:`downctl.session.policy_request` (the rule 0.1.5
    established). ``options`` are the keys of the ``[auth."<host>"]`` table
    that configured this provider — passed explicitly so one provider instance
    can serve several hosts with different settings.

    ``can_handle(host)`` is consulted by
    :class:`~downctl.core.credentials.AuthResolver` before ``material``: return
    ``False`` to decline a host, and ``material`` is not called and no material
    is resolved for that rule (that is an answer, not a failure; the next TTL
    window asks again). Raising is a failure: it is reported as an
    :class:`~downctl.core.credentials.AuthError` and not cached, so the next
    call retries. The argument is the lower-cased, IDNA-normalized hostname of
    the URL with no port or userinfo, so ``can_handle("example.com")`` covers
    ``https://user:pass@example.com:8443/x``. The built-in providers return
    ``True``: naming a provider in a rule already decides the host, and the hook
    lets a provider that serves only part of a domain say so without raising.
    """

    def can_handle(self, host: str) -> bool:
        """Whether this provider knows how to get material for ``host``."""
        ...

    async def material(
        self,
        host: str,
        ctx: RuntimeContext,
        options: Mapping[str, object],
    ) -> AuthMaterial | None:
        """Return the material for ``host``, or ``None`` when there is none."""
        ...


@runtime_checkable
class Server(Plugin, Protocol):
    """Serves a local file or directory for another machine to fetch.

    The contract says nothing about the wire protocol: the built-in ``http``
    server is one implementation, and a plugin speaking FTP or SFTP would be
    another. ``serve`` returns when the server stops (``SIGINT``/``SIGTERM``).
    """

    async def serve(
        self,
        root: str,
        ctx: RuntimeContext,
        options: Mapping[str, object],
    ) -> None:
        """Serve ``root`` until interrupted."""
        ...


# --------------------------------------------------------------------------- #
# optional capabilities
# --------------------------------------------------------------------------- #
# These are never used for registry validation (adding a member to a
# ``runtime_checkable`` Protocol would reject every third-party plugin that
# predates it). They exist so the optional behaviour is written down once and
# ``plugin check`` can report which ones an implementation has switched on.
# ``runtime_checkable`` checks presence, not value: every flag below means what
# it says only when it is ``True``.
@runtime_checkable
class SelfClosing(Protocol):
    """A hook that flushes work on shutdown (``aclose``)."""

    async def aclose(self) -> int: ...


@runtime_checkable
class MachineReadable(Protocol):
    """A reporter whose stdout is machine-readable JSON.

    ``runtime_checkable`` proves only that ``machine_readable`` exists; the
    value must be ``True`` for the promise to hold (``False`` describes a
    human-readable reporter and is not reported as a capability).
    """

    machine_readable: bool

    def failure(self, target: str, error: str, url: str = "", *, reason: str = "") -> None: ...


@runtime_checkable
class ContentRangeTransport(Protocol):
    """A transport whose ``206`` answers carry a trustworthy ``Content-Range``.

    ``sends_content_range`` must be ``True``: an attribute set to ``False``
    satisfies the structural check but means the opposite.
    """

    sends_content_range: bool


@runtime_checkable
class FilterableSource(Protocol):
    """A source whose listing can be narrowed with ``--include/--exclude``.

    ``supports_filters`` must be ``True``: an attribute set to ``False`` (the
    plain-URL source) satisfies the structural check but says the source has no
    listing to filter.
    """

    supports_filters: bool
