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

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from downctl import console
from downctl.builtin.sources._sigv4 import object_uri, presign_url
from downctl.config import as_bool
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin import credentials
from downctl.plugin.matching import filters, matches
from downctl.plugin.protocols import API_VERSION
from downctl.session import policy_request

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

SCHEME = "s3://"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
#: Upper bound on listing pages. A bucket with more keys than this is a mistake
#: in the target, not a listing to walk; the bound also stops a server that
#: keeps answering ``IsTruncated`` with the same token.
LIST_PAGE_MAX = 1000


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
        path_style = as_bool("plugins.s3.path_style", options.get("path_style"), True)
        try:
            expires = int(options.get("expires", 3600))
        except (TypeError, ValueError) as exc:
            raise DownctlError(
                f"[plugins.s3] expires must be an integer, got {options.get('expires')!r}"
            ) from exc
        # S3 rejects presigned URLs valid for more than 7 days.
        expires = max(1, min(expires, 604800))

        access_key = credentials.resolve(
            options, "access_key", credentials.AWS_ACCESS_KEY_ENVS
        ).value
        secret_key = credentials.resolve(
            options, "secret_key", credentials.AWS_SECRET_KEY_ENVS
        ).value
        session_token = credentials.resolve(
            options, "session_token", credentials.AWS_SESSION_TOKEN_ENVS
        ).value
        if not access_key or not secret_key:
            raise DownctlError(
                "S3 credentials missing: set [plugins.s3] access_key/secret_key "
                "or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
            )

        parts = urlsplit(endpoint)
        signer = _Signer(
            scheme=parts.scheme or "https",
            netloc=parts.netloc,
            # A gateway mounted under a path (``https://host/s3``) signs and
            # serves from that prefix. Dropping it produced 403 for every
            # request because the signature covered a path the server never
            # sees.
            base_path=parts.path.rstrip("/"),
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
                    # The engine re-signs through this when the transfer outlives
                    # the URL's lifetime; a 403 at that point is an expiry, not
                    # a permission problem.
                    meta={"redact_url": True, "refetch": lambda: signer.presign_get(bucket, key)},
                )
            ]

        include, exclude = filters(ctx)
        entries = await self._list_prefix(ctx, signer, bucket, key)
        tasks: list[DownloadTask] = []
        for object_key, size in entries:
            rel = object_key[len(key) :] if key and object_key.startswith(key) else object_key
            rel = rel.lstrip("/") or object_key.rsplit("/", 1)[-1]
            if not matches(rel, include, exclude):
                continue
            tasks.append(
                DownloadTask(
                    url=signer.presign_get(bucket, object_key),
                    target=rel,
                    size=size,
                    source=self.name,
                    meta={
                        "redact_url": True,
                        # Bind the key now: the loop variable moves on, and a
                        # late-bound closure would re-sign the wrong object.
                        "refetch": lambda _key=object_key: signer.presign_get(bucket, _key),
                    },
                )
            )
        if not tasks:
            raise DownctlError(f"no objects found under {target!r}")
        return tasks

    async def _head_size(self, ctx: RuntimeContext, signer: _Signer, bucket: str, key: str) -> int:
        """Return the object size, or 0 when it has to be probed.

        A rejection is *not* the same as an unknown size: ``401``/``403`` means
        the signature, credentials, region or endpoint are wrong, and saying so
        turns a silent "size unknown, will probe" into a message the user can
        act on. Anything else (a gateway that does not answer HEAD) is expected
        and only noted at ``-v``.
        """
        url = signer.presign_head(bucket, key)
        try:
            async with policy_request(
                ctx.session, "HEAD", url, policy=ctx.proxy_policy
            ) as response:
                if response.status == 200 and response.content_length is not None:
                    return int(response.content_length)
                if response.status in (401, 403):
                    console.warn(
                        f"s3: HEAD for s3://{bucket}/{key} was rejected "
                        f"(HTTP {response.status}); the size will be probed instead. "
                        "Check [plugins.s3] credentials, region and endpoint."
                    )
                else:
                    console.verbose(
                        f"s3: HEAD for s3://{bucket}/{key} answered HTTP {response.status}; "
                        "the size will be probed instead"
                    )
        except Exception as exc:  # noqa: BLE001 - the probe path still applies
            console.verbose(f"s3: HEAD for {bucket}/{key} failed ({exc}); will probe instead")
        return 0

    async def _list_prefix(
        self, ctx: RuntimeContext, signer: _Signer, bucket: str, prefix: str
    ) -> list[tuple[str, int]]:
        entries: list[tuple[str, int]] = []
        seen_keys: set[str] = set()
        token = ""
        seen_tokens: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > LIST_PAGE_MAX:
                raise DownctlError(
                    f"the listing for s3://{bucket}/{prefix} did not finish after "
                    f"{LIST_PAGE_MAX} pages; narrow the prefix"
                )
            extra = {"list-type": "2", "prefix": prefix}
            if token:
                extra["continuation-token"] = token
            url = signer.presign_list(bucket, extra)
            async with policy_request(ctx.session, "GET", url, policy=ctx.proxy_policy) as response:
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
                object_key = key_el.text
                # A listing may contain directory markers (``dir/``). They name
                # a prefix, not an object, and make a task whose basename is
                # empty -- one that can never download. Zero-length keys are
                # filtered for the same reason.
                if object_key.endswith("/") or not object_key.rsplit("/", 1)[-1]:
                    continue
                # A listing is a set of keys. A server that hands out a fresh
                # continuation token each page while replaying the same keys
                # would slip past the token check and produce a task per
                # duplicate (five entries for three keys, reproduced).
                if object_key in seen_keys:
                    continue
                seen_keys.add(object_key)
                size = int(size_el.text) if size_el is not None and size_el.text else 0
                entries.append((object_key, size))
            truncated = (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() == "true"
            next_token = root.findtext(f"{S3_NS}NextContinuationToken") or ""
            if not truncated or not next_token:
                break
            if next_token in seen_tokens:
                # A server that replays a token would otherwise loop forever,
                # appending the same keys to the task list each time.
                raise DownctlError(
                    f"the listing for s3://{bucket}/{prefix} repeated a continuation token; "
                    "aborting instead of looping"
                )
            seen_tokens.add(next_token)
            token = next_token
        return entries


class _Signer:
    def __init__(
        self,
        *,
        scheme: str,
        netloc: str,
        base_path: str,
        region: str,
        access_key: str,
        secret_key: str,
        session_token: str,
        path_style: bool,
        expires: int,
    ) -> None:
        self.scheme = scheme
        self.netloc = netloc
        #: Path the endpoint is mounted under (``/s3``), part of both the signed
        #: canonical URI and the URL that is actually requested.
        self.base_path = base_path
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
            canonical_uri=f"{self.base_path}{uri}",
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
