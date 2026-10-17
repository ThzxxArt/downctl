"""Built-in plugins: hooks."""

from __future__ import annotations

from downctl.builtin.hooks.archive_hook import ArchiveHook
from downctl.builtin.hooks.checksum_hook import ChecksumHook
from downctl.builtin.hooks.exec_hook import ExecHook
from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.hooks.manifest_hook import ManifestHook
from downctl.builtin.hooks.webhook_hook import WebhookHook

__all__ = [
    "LogfileHook",
    "ArchiveHook",
    "WebhookHook",
    "ManifestHook",
    "ExecHook",
    "ChecksumHook",
]
