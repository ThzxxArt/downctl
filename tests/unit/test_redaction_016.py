"""What may be printed: the redaction rules, not the shapes they happen to hit.

``is_secret_name`` used substring hints, which masked a boolean
(``token_to_mirror``) and missed real credentials (``code``, ``jwt``,
``session``) — and ``design`` matched ``sig``.
"""

from __future__ import annotations

import pytest

from downctl.urls import display_url, is_secret_name, mask_mapping


@pytest.mark.parametrize(
    "name",
    ["token", "HF_TOKEN", "api-key", "X-Amz-Signature", "code", "jwt", "session_id", "password"],
)
def test_credential_bearing_names_are_recognised(name: str) -> None:
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["token_to_mirror", "design", "author", "keyring", "jobs"])
def test_names_that_only_look_secret_are_not_masked(name: str) -> None:
    assert not is_secret_name(name)


def test_a_path_credential_is_masked_after_its_named_segment() -> None:
    assert display_url("https://h/token/SECRET/x.bin") == "https://h/token/***/x.bin"
    assert display_url("https://h/v1/models/llama") == "https://h/v1/models/llama"


def test_mask_mapping_keeps_token_to_mirror_readable() -> None:
    masked = mask_mapping({"token_to_mirror": False, "code": "SECRET"})
    assert masked == {"token_to_mirror": False, "code": "***"}
