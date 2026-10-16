"""Built-in plugins: sources."""

from __future__ import annotations

from downctl.builtin.sources.github_source import GitHubSource
from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.builtin.sources.s3_source import S3Source
from downctl.builtin.sources.sftp_source import SFTPSource
from downctl.builtin.sources.url_source import URLSource

__all__ = ["URLSource", "HuggingFaceSource", "GitHubSource", "S3Source", "SFTPSource"]
