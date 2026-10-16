"""Unit tests for the HuggingFace source (handling and parsing)."""

from __future__ import annotations

import pytest

from downctl.builtin.sources.huggingface_source import HuggingFaceSource, _parse_target
from downctl.config import Config
from downctl.core.context import RuntimeContext


class _DummyCtx:
    """Minimal context exposing only ``params`` for parsing tests."""

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}


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
    kind, repo, rev = _parse_target("hf://org/name@v2", _DummyCtx())
    assert (kind, repo, rev) == ("models", "org/name", "v2")


def test_parse_target_revision_from_params() -> None:
    kind, repo, rev = _parse_target("hf://org/name", _DummyCtx({"revision": "dev"}))
    assert (kind, repo, rev) == ("models", "org/name", "dev")


def test_parse_target_datasets() -> None:
    kind, repo, _ = _parse_target("hf://datasets/squad", _DummyCtx())
    assert (kind, repo) == ("datasets", "squad")


def test_parse_target_invalid() -> None:
    with pytest.raises(Exception, match="invalid HuggingFace repo id"):
        _parse_target("hf://", _DummyCtx())


def test_plugin_options_default_host() -> None:
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(),
        options={"huggingface": {"mirror": "https://hf-mirror.com"}},
    )
    assert ctx.plugin_options("huggingface")["mirror"] == "https://hf-mirror.com"
    assert ctx.plugin_options("missing") == {}
