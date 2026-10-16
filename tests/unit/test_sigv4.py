"""Tests for SigV4 presigning helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from downctl.builtin.sources._sigv4 import object_uri, presign_url

FIXED = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


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
    kwargs = {
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
