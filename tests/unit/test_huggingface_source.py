"""Unit tests for the HuggingFace source (handling and parsing)."""

from __future__ import annotations

from typing import cast

import aiohttp
import pytest

from downctl.builtin.sources.huggingface_source import HuggingFaceSource, _parse_target
from downctl.config import Config
from downctl.core.context import RuntimeContext


def _ctx(params: dict[str, object] | None = None) -> RuntimeContext:
    """A real context for the parser tests, which only read ``params``.

    The session is never opened here, so the cast states the stand-in rather
    than pretending a bare ``object()`` is an aiohttp session.
    """
    return RuntimeContext(
        session=cast("aiohttp.ClientSession", None),
        config=Config(),
        params=dict(params or {}),
    )


@pytest.mark.parametrize(
    "target",
    ["hf://org/name", "hf://datasets/org/name", "org/name", "datasets/org/name", "org/name@dev"],
)
def test_can_handle_true(target: str) -> None:
    assert HuggingFaceSource().can_handle(target)


@pytest.mark.parametrize(
    "target",
    ["https://example.com/a.bin", "/abs/path/file", "org/name/extra", "./local"],
)
def test_can_handle_false(target: str) -> None:
    assert not HuggingFaceSource().can_handle(target)


def test_parse_target_revision_from_at_sign() -> None:
    kind, repo, rev = _parse_target("hf://org/name@v2", _ctx())
    assert (kind, repo, rev) == ("models", "org/name", "v2")


def test_parse_target_revision_from_params() -> None:
    kind, repo, rev = _parse_target("hf://org/name", _ctx({"revision": "dev"}))
    assert (kind, repo, rev) == ("models", "org/name", "dev")


def test_parse_target_datasets() -> None:
    kind, repo, _ = _parse_target("hf://datasets/squad", _ctx())
    assert (kind, repo) == ("datasets", "squad")


def test_parse_target_invalid() -> None:
    with pytest.raises(Exception, match="invalid HuggingFace repo id"):
        _parse_target("hf://", _ctx())


def test_plugin_options_default_host() -> None:
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(),
        options={"huggingface": {"mirror": "https://hf-mirror.com"}},
    )
    assert ctx.plugin_options("huggingface")["mirror"] == "https://hf-mirror.com"
    assert ctx.plugin_options("missing") == {}


def test_the_token_goes_to_the_host_that_issued_it() -> None:
    """A ``mirror`` pointing at huggingface.co is not a different host.

    Asking "is a mirror configured at all" withheld the token from the API that
    issued it, turning a private repository into a 401 that advised raising
    ``token_to_mirror`` — the opposite of the problem.
    """
    from downctl.builtin.sources.huggingface_source import sends_token_to

    assert sends_token_to("https://huggingface.co", to_mirror=False) is True
    assert sends_token_to("https://huggingface.co/", to_mirror=False) is True
    assert sends_token_to("https://hf-mirror.com", to_mirror=False) is False
    assert sends_token_to("https://hf-mirror.com", to_mirror=True) is True
