"""The S3 source plugin.

Expands ``s3://<bucket>/<key>`` (a single object) or ``s3://<bucket>/<prefix>/``
(a prefix listing) into download tasks. Objects are fetched through presigned
SigV4 URLs, so the ordinary HTTP transport handles them (including ranged,
multi-connection transfers) with no S3-specific dependency.

Options are read from ``[plugins.s3]`` (or the standard AWS environment
variables):

- ``endpoint``:      base endpoint, default ``https://s3.<region>.amazonaws.com``
- ``region``:        signing region, default ``us-east-1``
- ``access_key`` / ``secret_key`` / ``session_token``
- ``path_style``:    path-style addressing (default ``true``)
- ``expires``:       presigned URL lifetime in seconds (default ``3600``)
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from downctl.builtin.sources._sigv4 import object_uri, presign_url
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

SCHEME = "s3://"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Source:
    """Expand an S3 object or prefix into download tasks."""

    name = "s3"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        return target.startswith(SCHEME)

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        bucket, key = _parse_target(target)
        options = ctx.plugin_options(self.name)
        region = str(options.get("region") or "us-east-1")
        endpoint = str(options.get("endpoint") or f"https://s3.{region}.amazonaws.com")
        path_style = bool(options.get("path_style", True))
        try:
            expires = int(options.get("expires", 3600))
        except (TypeError, ValueError) as exc:
            raise DownctlError(
                f"[plugins.s3] expires must be an integer, got {options.get('expires')!r}"
            ) from exc
        # S3 rejects presigned URLs valid for more than 7 days.
        expires = max(1, min(expires, 604800))

        access_key = str(options.get("access_key") or os.environ.get("AWS_ACCESS_KEY_ID", ""))
        secret_key = str(options.get("secret_key") or os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
        session_token = str(options.get("session_token") or os.environ.get("AWS_SESSION_TOKEN", ""))
        if not access_key or not secret_key:
            raise DownctlError(
                "S3 credentials missing: set [plugins.s3] access_key/secret_key "
                "or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
            )

        parts = urlsplit(endpoint)
        signer = _Signer(
            scheme=parts.scheme or "https",
            netloc=parts.netloc,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            path_style=path_style,
            expires=expires,
        )

        if key and not key.endswith("/"):
            url = signer.presign_get(bucket, key)
            size = await self._head_size(ctx, signer, bucket, key)
            return [
                DownloadTask(
                    url=url,
                    target=key.rsplit("/", 1)[-1],
                    size=size,
                    source=self.name,
                    meta={"redact_url": True},
                )
            ]

        include = list(ctx.params.get("include") or [])
        exclude = list(ctx.params.get("exclude") or [])
        entries = await self._list_prefix(ctx, signer, bucket, key)
        tasks: list[DownloadTask] = []
        for object_key, size in entries:
            rel = object_key[len(key) :] if key and object_key.startswith(key) else object_key
            rel = rel.lstrip("/") or object_key.rsplit("/", 1)[-1]
            if include and not any(_match(rel, pat) for pat in include):
                continue
            if any(_match(rel, pat) for pat in exclude):
                continue
            tasks.append(
                DownloadTask(
                    url=signer.presign_get(bucket, object_key),
                    target=rel,
                    size=size,
                    source=self.name,
                    meta={"redact_url": True},
                )
            )
        if not tasks:
            raise DownctlError(f"no objects found under {target!r}")
        return tasks

    async def _head_size(self, ctx: RuntimeContext, signer: _Signer, bucket: str, key: str) -> int:
        """Return the object size via a presigned HEAD, or 0 when unknown."""
        url = signer.presign_head(bucket, key)
        try:
            async with ctx.session.head(url, allow_redirects=True) as response:
                if response.status == 200 and response.content_length is not None:
                    return int(response.content_length)
        except Exception:  # noqa: BLE001 - fall back to probing
            pass
        return 0

    async def _list_prefix(
        self, ctx: RuntimeContext, signer: _Signer, bucket: str, prefix: str
    ) -> list[tuple[str, int]]:
        entries: list[tuple[str, int]] = []
        token = ""
        while True:
            extra = {"list-type": "2", "prefix": prefix}
            if token:
                extra["continuation-token"] = token
            url = signer.presign_list(bucket, extra)
            async with ctx.session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    body = await response.text()
                    raise DownctlError(
                        f"cannot list s3://{bucket}/{prefix} (HTTP {response.status})\n{body[:300]}"
                    )
                text = await response.text()
            try:
                root = ET.fromstring(text)  # noqa: S314 - trusted S3 XML response
            except ET.ParseError as exc:
                raise DownctlError(
                    f"cannot parse S3 listing for s3://{bucket}/{prefix}: {exc}"
                ) from exc
            for content in root.findall(f"{S3_NS}Contents"):
                key_el = content.find(f"{S3_NS}Key")
                size_el = content.find(f"{S3_NS}Size")
                if key_el is None or not key_el.text:
                    continue
                size = int(size_el.text) if size_el is not None and size_el.text else 0
                entries.append((key_el.text, size))
            truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
            token = root.findtext(f"{S3_NS}NextContinuationToken") or ""
            if not truncated or not token:
                break
        return entries


class _Signer:
    def __init__(
        self,
        *,
        scheme: str,
        netloc: str,
        region: str,
        access_key: str,
        secret_key: str,
        session_token: str,
        path_style: bool,
        expires: int,
    ) -> None:
        self.scheme = scheme
        self.netloc = netloc
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.path_style = path_style
        self.expires = expires

    def _host(self, bucket: str) -> str:
        if self.path_style:
            return self.netloc
        return f"{bucket}.{self.netloc}"

    def _presign(self, bucket: str, method: str, uri: str, extra: dict[str, str]) -> str:
        return presign_url(
            method=method,
            scheme=self.scheme,
            host=self._host(bucket),
            canonical_uri=uri,
            region=self.region,
            access_key=self.access_key,
            secret_key=self.secret_key,
            session_token=self.session_token,
            expires=self.expires,
            extra_query=extra,
        )

    def presign_get(self, bucket: str, key: str) -> str:
        uri = object_uri(bucket, key, path_style=self.path_style)
        return self._presign(bucket, "GET", uri, {})

    def presign_head(self, bucket: str, key: str) -> str:
        uri = object_uri(bucket, key, path_style=self.path_style)
        return self._presign(bucket, "HEAD", uri, {})

    def presign_list(self, bucket: str, extra: dict[str, str]) -> str:
        uri = object_uri(bucket, "", path_style=self.path_style)
        return self._presign(bucket, "GET", uri, extra)


def _parse_target(target: str) -> tuple[str, str]:
    raw = target[len(SCHEME) :] if target.startswith(SCHEME) else target
    bucket, _, key = raw.partition("/")
    if not bucket:
        raise DownctlError(f"invalid S3 target: {target!r} (expected s3://bucket/key)")
    return bucket, key


def _match(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)
