"""The HuggingFace source plugin.

Expands ``hf://<repo>[@<revision>]`` (and bare ``org/name``) into download
tasks by querying the Hub API. Supports models and datasets, revision pinning,
include/exclude globs and LFS metadata (size + sha256).

Options are read from the ``[plugins.huggingface]`` namespace via the runtime
context:

- ``mirror``: base host to use instead of ``https://huggingface.co``
  (e.g. ``https://hf-mirror.com``)
- ``token``:  access token, attached to the host that issued it
  (``huggingface.co``) and, only when ``token_to_mirror`` is enabled, to a
  configured mirror as well
- ``token_to_mirror``: send the token to the mirror too (default ``false``)

Targets:

- ``hf://meta-llama/Llama-3-8B``
- ``hf://datasets/squad@main``
- ``org/name`` (bare, when the path does not exist locally)
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import aiohttp

from downctl import console
from downctl.config import as_bool
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin import credentials
from downctl.plugin.matching import filters, matches
from downctl.plugin.protocols import API_VERSION
from downctl.session import policy_request

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

DEFAULT_HOST = "https://huggingface.co"
SCHEME = "hf://"
_BARE_RE = re.compile(r"^(?:datasets/)?[\w.\-]+/[\w.\-]+(?:@[\w.\-/]+)?$")


class HuggingFaceSource:
    """Expand a HuggingFace repository into download tasks."""

    name = "huggingface"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        if target.startswith(SCHEME):
            return True
        if "://" in target or os.path.exists(target):
            return False
        if target.startswith((".", "/", "\\")):
            return False
        return bool(_BARE_RE.match(target))

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        kind, repo, revision = _parse_target(target, ctx)
        options = ctx.plugin_options(self.name)
        mirror = str(options.get("mirror") or "").rstrip("/")
        host = mirror or DEFAULT_HOST
        include, exclude = filters(ctx)
        token = credentials.resolve(options, "token", credentials.HF_TOKEN_ENVS).value
        # A token is a credential for the host that issued it. A configured
        # mirror is a different host, so handing it the token is a separate
        # decision that has to be made explicitly; ``mirror`` alone used to
        # imply it, which sent the credential to whatever URL was configured.
        # One decision, computed once, used by both the listing and the tasks so
        # the two can never disagree. Read strictly: ``bool("false")`` is true,
        # and this value decides whether a credential leaves its host.
        to_mirror = as_bool(
            "plugins.huggingface.token_to_mirror",
            options.get("token_to_mirror"),
            False,
        )
        # A mirror that *is* huggingface.co is not a different host, so the token
        # still belongs to the origin it is sent to. Testing "is a mirror
        # configured at all" withheld the credential from the API that issued it,
        # which turned a private repo into a 401 with the opposite advice.
        send_token = bool(token) and sends_token_to(host, to_mirror=to_mirror)
        auth_headers = {"Authorization": f"Bearer {token}"} if send_token else {}
        withheld_hint = ""
        if token and not send_token:
            withheld_hint = (
                f"The HuggingFace token was not sent to {host}: it belongs to "
                f"{DEFAULT_HOST}. Set [plugins.huggingface] token_to_mirror = true "
                "to allow sending it there."
            )
            console.verbose(
                f"the HuggingFace token is not sent to mirror {host}; "
                "set [plugins.huggingface] token_to_mirror = true to allow it"
            )

        api = f"{host}/api/{kind}/{quote(repo, safe='/')}?blobs=true"
        if revision != "main":
            api += f"&revision={quote(revision, safe='')}"

        async with policy_request(
            ctx.session, "GET", api, policy=ctx.proxy_policy, headers=auth_headers
        ) as response:
            if response.status != 200:
                body = await response.text()
                hint = f"\n{withheld_hint}" if withheld_hint else ""
                raise DownctlError(
                    f"cannot list repo {repo!r} (HTTP {response.status}, {api}). "
                    f"Check the repo id / revision / token.{hint}\n{body[:300]}"
                )
            try:
                data = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise DownctlError(f"repo listing is not valid JSON ({api})") from exc

        siblings = data.get("siblings", []) if isinstance(data, dict) else []
        tasks: list[DownloadTask] = []
        for sibling in siblings:
            rel = sibling.get("rfilename", "")
            if not rel:
                continue
            if not matches(rel, include, exclude):
                continue

            encoded = "/".join(quote(segment, safe="") for segment in rel.split("/"))
            prefix = "" if kind == "models" else f"/{kind}"
            url = f"{host}{prefix}/{repo}/resolve/{quote(revision, safe='')}/{encoded}"

            size = 0
            checksum = ""
            lfs = sibling.get("lfs")
            if isinstance(lfs, dict):
                if isinstance(lfs.get("size"), int):
                    size = lfs["size"]
                sha = lfs.get("sha256")
                if isinstance(sha, str) and sha:
                    checksum = sha
            elif isinstance(sibling.get("size"), int):
                size = sibling["size"]

            tasks.append(
                DownloadTask(
                    url=url,
                    target=rel,
                    size=size,
                    checksum_algo="sha256" if checksum else "",
                    checksum_hex=checksum,
                    source=self.name,
                    headers=dict(auth_headers),
                    # Carried so a 401 from the *probe* — the first request the
                    # download makes — explains the withheld token instead of
                    # leaving the user with a bare status.
                    meta={"credential_hint": withheld_hint} if withheld_hint else {},
                )
            )

        if not tasks:
            raise DownctlError(
                f"repo {repo!r} (revision={revision}) has no matching files; "
                f"check --include/--exclude or --revision"
            )
        return tasks


def sends_token_to(host: str, *, to_mirror: bool) -> bool:
    """Whether the HuggingFace token may be sent to ``host``.

    The token belongs to the host that issued it. A ``mirror`` that points at
    ``huggingface.co`` is that host — different from "a mirror is configured",
    which is what the previous test asked. A genuinely different host needs the
    explicit ``token_to_mirror`` opt-in.
    """
    target_host = urlsplit(host).netloc.lower()
    return target_host == urlsplit(DEFAULT_HOST).netloc.lower() or to_mirror


def _parse_target(target: str, ctx: RuntimeContext) -> tuple[str, str, str]:
    raw = target[len(SCHEME) :] if target.startswith(SCHEME) else target
    kind = "models"
    if raw.startswith("datasets/"):
        kind = "datasets"
        raw = raw[len("datasets/") :]

    revision = str(ctx.params.get("revision") or "main")
    if "@" in raw:
        raw, _, pinned = raw.rpartition("@")
        revision = pinned or revision

    repo = raw.strip("/")
    if not repo:
        raise DownctlError(f"invalid HuggingFace repo id: {target!r}")
    return kind, repo, revision
