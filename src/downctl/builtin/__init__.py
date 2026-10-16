"""Built-in plugins.

These ship with downctl but use the exact same plugin interface as
third-party plugins, so any of them can be shadowed or replaced by name.
"""

from __future__ import annotations

from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.builtin.routers.mirror_router import MirrorRouter
from downctl.builtin.sinks.filesystem_sink import FilesystemSink
from downctl.builtin.sources.github_source import GitHubSource
from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.builtin.sources.s3_source import S3Source
from downctl.builtin.sources.url_source import URLSource
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.builtin.verifiers.sha256_verifier import SHA256Verifier

#: category -> list of plugin classes
BUILTIN_PLUGINS: dict[str, list[type]] = {
    "source": [URLSource, HuggingFaceSource, GitHubSource, S3Source],
    "transport": [HTTPTransport],
    "verifier": [SHA256Verifier],
    "sink": [FilesystemSink],
    "reporter": [TqdmReporter, JSONReporter, QuietReporter],
    "router": [MirrorRouter],
    "hook": [LogfileHook],
}


def register_all(registry: object, *, origin: str = "builtin") -> None:
    """Register every built-in plugin into ``registry``."""
    for category, classes in BUILTIN_PLUGINS.items():
        for cls in classes:
            registry.register(category, cls(), origin=f"{origin}:{cls.__name__}")  # type: ignore[attr-defined]


__all__ = ["BUILTIN_PLUGINS", "register_all"]
