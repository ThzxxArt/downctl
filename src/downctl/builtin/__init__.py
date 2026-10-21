"""Built-in plugins.

These ship with downctl but use the exact same plugin interface as
third-party plugins, so any of them can be shadowed or replaced by name.

Registration happens **only here**. Before 0.1.6 every built-in was also
listed as an entry point in ``pyproject.toml``, so each one was instantiated
twice and the second registration silently replaced the first — ``origin``
reported the entry point, a typo in that path was hidden by the code path, and
``plugin info`` could not say where a plugin came from.
"""

from __future__ import annotations

from downctl.builtin.auths import CommandAuth, EnvAuth, NetrcAuth
from downctl.builtin.hooks.archive_hook import ArchiveHook
from downctl.builtin.hooks.checksum_hook import ChecksumHook
from downctl.builtin.hooks.exec_hook import ExecHook
from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.hooks.manifest_hook import ManifestHook
from downctl.builtin.hooks.metrics_hook import MetricsHook
from downctl.builtin.hooks.notify_hook import NotifyHook
from downctl.builtin.hooks.webhook_hook import WebhookHook
from downctl.builtin.jobsources import JsonJobSource, TomlJobSource
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.builtin.routers.mirror_router import MirrorRouter
from downctl.builtin.servers import HTTPFileServer
from downctl.builtin.sources.file_source import FileSource
from downctl.builtin.sources.github_source import GitHubSource
from downctl.builtin.sources.http_index_source import HTTPIndexSource
from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.builtin.sources.s3_source import S3Source
from downctl.builtin.sources.sftp_source import SFTPSource
from downctl.builtin.sources.url_source import URLSource
from downctl.builtin.transports.file_transport import FileTransport
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.builtin.transports.sftp_transport import SFTPTransport
from downctl.builtin.verifiers.digest_verifier import MD5Verifier, SHA1Verifier, SHA512Verifier
from downctl.builtin.verifiers.sha256_verifier import SHA256Verifier

#: category -> list of plugin classes
BUILTIN_PLUGINS: dict[str, list[type]] = {
    "source": [
        URLSource,
        HTTPIndexSource,
        HuggingFaceSource,
        GitHubSource,
        S3Source,
        SFTPSource,
        FileSource,
    ],
    "transport": [HTTPTransport, SFTPTransport, FileTransport],
    "verifier": [SHA256Verifier, SHA1Verifier, SHA512Verifier, MD5Verifier],
    "reporter": [TqdmReporter, JSONReporter, QuietReporter],
    "router": [MirrorRouter],
    "hook": [
        LogfileHook,
        ArchiveHook,
        WebhookHook,
        ManifestHook,
        ExecHook,
        ChecksumHook,
        MetricsHook,
        NotifyHook,
    ],
    "jobsource": [TomlJobSource, JsonJobSource],
    "auth": [EnvAuth, CommandAuth, NetrcAuth],
    "server": [HTTPFileServer],
}


def register_all(registry: object, *, origin: str = "builtin") -> None:
    """Register every built-in plugin into ``registry``."""
    for category, classes in BUILTIN_PLUGINS.items():
        for cls in classes:
            registry.register(category, cls(), origin=f"{origin}:{cls.__name__}")  # type: ignore[attr-defined]


__all__ = ["BUILTIN_PLUGINS", "register_all"]
