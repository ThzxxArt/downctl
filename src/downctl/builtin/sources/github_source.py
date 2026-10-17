"""The GitHub Releases source plugin.

Expands ``gh://<owner>/<repo>[@<tag>]`` into download tasks by listing the
release assets via the GitHub REST API. Without a tag the latest release is
used. Asset digests (``sha256:*``) are used for verification when present.

Options are read from ``[plugins.github]``:

- ``token``: API token (also honoured via ``GITHUB_TOKEN`` / ``GH_TOKEN``)
- ``api``:   API base URL, e.g. a GitHub Enterprise instance
"""

from __future__ import annotations

import fnmatch
import os
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

SCHEME = "gh://"
DEFAULT_API = "https://api.github.com"


class GitHubSource:
    """Expand a GitHub release into download tasks (one per asset)."""

    name = "github"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        return target.startswith(SCHEME)

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        owner, repo, tag = _parse_target(target)
        options = ctx.plugin_options(self.name)
        api = str(options.get("api") or DEFAULT_API).rstrip("/")
        token = (
            str(options.get("token") or "")
            or os.environ.get("GITHUB_TOKEN", "")
            or os.environ.get("GH_TOKEN", "")
        )
        include = list(ctx.params.get("include") or [])
        exclude = list(ctx.params.get("exclude") or [])

        endpoint = (
            f"{api}/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"
            if tag
            else f"{api}/repos/{owner}/{repo}/releases/latest"
        )
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with ctx.session.get(endpoint, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                body = await response.text()
                raise DownctlError(
                    f"cannot list release for {owner}/{repo} (HTTP {response.status}). "
                    f"Check the repo/tag or provide a token.\n{body[:300]}"
                )
            try:
                data = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise DownctlError(f"release listing is not valid JSON ({endpoint})") from exc

        assets = data.get("assets", []) if isinstance(data, dict) else []
        tasks: list[DownloadTask] = []
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url", "")
            if not name or not url:
                continue
            if include and not any(fnmatch.fnmatchcase(name, pat) for pat in include):
                continue
            if any(fnmatch.fnmatchcase(name, pat) for pat in exclude):
                continue

            checksum = ""
            digest = asset.get("digest")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                checksum = digest[len("sha256:") :]
            size = asset.get("size") if isinstance(asset.get("size"), int) else 0

            tasks.append(
                DownloadTask(
                    url=url,
                    target=name,
                    size=size,
                    checksum_algo="sha256" if checksum else "",
                    checksum_hex=checksum,
                    source=self.name,
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                )
            )

        if not tasks:
            label = tag or "latest"
            raise DownctlError(
                f"release {owner}/{repo}@{label} has no matching assets; check --include/--exclude"
            )
        return tasks


def _parse_target(target: str) -> tuple[str, str, str]:
    raw = target[len(SCHEME) :] if target.startswith(SCHEME) else target
    tag = ""
    if "@" in raw:
        raw, _, tag = raw.rpartition("@")
    parts = raw.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise DownctlError(f"invalid GitHub repo: {target!r} (expected gh://owner/repo[@tag])")
    return parts[0], parts[1], tag
