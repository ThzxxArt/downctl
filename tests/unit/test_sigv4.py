"""Tests for SigV4 presigning helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from downctl.builtin.sources._sigv4 import object_uri, presign_url

FIXED = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


class _PresignArgs(TypedDict):
    """The keyword table ``presign_url`` accepts.

    Typed rather than ``dict[str, object]`` so renaming or retyping a parameter
    fails the tests instead of silently splatting an unknown key.
    """

    method: str
    scheme: str
    host: str
    canonical_uri: str
    region: str
    access_key: str
    secret_key: str
    session_token: NotRequired[str]
    expires: NotRequired[int]
    extra_query: NotRequired[dict[str, str]]
    now: NotRequired[datetime]


def test_object_uri_path_style() -> None:
    assert object_uri("bkt", "a/b c.txt", path_style=True) == "/bkt/a/b%20c.txt"
    assert object_uri("bkt", "", path_style=True) == "/bkt"


def test_object_uri_virtual_host() -> None:
    assert object_uri("bkt", "a/b.txt", path_style=False) == "/a/b.txt"


def test_presign_url_structure() -> None:
    url = presign_url(
        method="GET",
        scheme="https",
        host="s3.us-east-1.amazonaws.com",
        canonical_uri="/bkt/a.txt",
        region="us-east-1",
        access_key="AKID",
        secret_key="SECRET",
        now=FIXED,
        extra_query={},
    )
    assert url.startswith("https://s3.us-east-1.amazonaws.com/bkt/a.txt?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Credential=AKID%2F20240101%2Fus-east-1%2Fs3%2Faws4_request" in url
    assert "X-Amz-Date=20240101T120000" in url
    assert "X-Amz-SignedHeaders=host" in url
    assert re.search(r"X-Amz-Signature=[0-9a-f]{64}$", url)


def test_presign_is_deterministic() -> None:
    kwargs: _PresignArgs = {
        "method": "GET",
        "scheme": "https",
        "host": "h",
        "canonical_uri": "/b/k",
        "region": "us-east-1",
        "access_key": "a",
        "secret_key": "s",
        "now": FIXED,
    }
    assert presign_url(**kwargs) == presign_url(**kwargs)


def test_session_token_included() -> None:
    url = presign_url(
        method="GET",
        scheme="https",
        host="h",
        canonical_uri="/b/k",
        region="us-east-1",
        access_key="a",
        secret_key="s",
        session_token="TOK",
        now=FIXED,
    )
    assert "X-Amz-Security-Token=TOK" in url


# --------------------------------------------------------------------------- #
# the published test vector
# --------------------------------------------------------------------------- #
#: AWS's documented "Example: GET Object" presigned URL (Authenticating
#: Requests: Using Query Parameters). Every one of these values is fixed by the
#: documentation, including the expected signature, so this test proves the
#: canonical request, the string to sign and the signing key chain at once.
AWS_EXAMPLE: _PresignArgs = {
    "method": "GET",
    "scheme": "https",
    "host": "examplebucket.s3.amazonaws.com",
    "canonical_uri": "/test.txt",
    "region": "us-east-1",
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "expires": 86400,
    "now": datetime(2013, 5, 24, tzinfo=UTC),
}
AWS_EXAMPLE_SIGNATURE = "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"


def _presigned(**overrides: Any) -> str:
    """The published example with a partial change applied.

    ``Any`` here is the honest type for a partial override table: the keys and
    their types are checked by ``_PresignArgs`` on the full tables above, while
    a change names one field at a time and cannot be expressed as a complete
    ``TypedDict``.
    """
    return presign_url(**{**AWS_EXAMPLE, **overrides})


def test_the_presigned_signature_matches_the_published_aws_example() -> None:
    """Without a published vector, "the signer works" was an untested claim.

    0.1.4 shipped SigV4 with tests that only checked for the *shape* of the URL
    (``X-Amz-Signature=<64 hex>``), so a wrong canonical request or a missing
    key-derivation step would have passed. This one compares against the value
    AWS publishes.
    """
    url = presign_url(**AWS_EXAMPLE)
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z"
        "&X-Amz-Expires=86400"
        "&X-Amz-SignedHeaders=host"
        f"&X-Amz-Signature={AWS_EXAMPLE_SIGNATURE}"
    )


def test_the_signature_covers_every_parameter_it_promises_to() -> None:
    """Each signed input must change the signature when it changes."""
    baseline = re.search(r"X-Amz-Signature=(\w+)$", presign_url(**AWS_EXAMPLE))

    for change in (
        {"expires": 86401},
        {"canonical_uri": "/other.txt"},
        {"region": "eu-west-1"},
        {"secret_key": "different"},
        {"extra_query": {"prefix": "logs/"}},
        {"session_token": "TOKEN"},
    ):
        other = re.search(r"X-Amz-Signature=(\w+)$", _presigned(**change))
        assert other is not None
        assert other.group(1) != baseline.group(1), f"{change} did not affect the signature"
