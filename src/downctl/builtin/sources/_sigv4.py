"""AWS Signature Version 4 query-string presigning (S3 subset).

Produces presigned URLs so the ordinary HTTP transport can fetch objects
(including ranged, multi-connection requests) without S3-specific code. Only
the pieces needed for ``GET`` object/list requests are implemented.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    safe = "-_.~" if encode_slash else "-_.~/"
    return quote(value, safe=safe)


def _canonical_query(params: dict[str, str]) -> str:
    encoded = [
        (_uri_encode(k, encode_slash=True), _uri_encode(v, encode_slash=True))
        for k, v in params.items()
    ]
    encoded.sort()
    return "&".join(f"{k}={v}" for k, v in encoded)


def presign_url(
    *,
    method: str,
    scheme: str,
    host: str,
    canonical_uri: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str = "",
    expires: int = 3600,
    extra_query: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Return a presigned URL for ``method`` on the given host/URI."""
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{region}/s3/aws4_request"

    query: dict[str, str] = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if session_token:
        query["X-Amz-Security-Token"] = session_token
    if extra_query:
        query.update(extra_query)

    canonical_query = _canonical_query(query)
    canonical_headers = f"host:{host}\n"
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host",
            UNSIGNED_PAYLOAD,
        ]
    )
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    signing_key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date_stamp), region), "s3"),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return f"{scheme}://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def object_uri(bucket: str, key: str, *, path_style: bool) -> str:
    """Return the canonical request URI for an object key."""
    if path_style:
        return "/" + bucket + "/" + _uri_encode(key, encode_slash=False) if key else "/" + bucket
    return "/" + _uri_encode(key, encode_slash=False) if key else "/"
