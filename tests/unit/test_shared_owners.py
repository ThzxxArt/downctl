"""The shared include/exclude rule and the shared credential resolver.

Both exist because five sources answered the same question with five copies of
the same code. These tests pin the behaviour that every source now inherits.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.digest import hash_file, supported
from downctl.plugin import credentials
from downctl.plugin.matching import filters, matches


# --- include / exclude ----------------------------------------------------- #
def test_an_empty_include_list_accepts_everything() -> None:
    assert matches("sub/model.bin", [], [])
    assert matches("model.bin", [], [])


def test_include_filters_and_exclude_wins() -> None:
    include = ["*.bin"]
    assert matches("model.bin", include, [])
    assert not matches("model.json", include, [])
    # Exclude beats include, whatever the pattern specificity.
    assert not matches("model.bin", include, ["*.bin"])
    assert not matches("sub/model.bin", ["sub/*"], ["sub/model.bin"])


def test_patterns_match_the_relative_path_not_the_basename() -> None:
    """``sub/*.bin`` must not match a top-level file, and vice versa."""
    assert matches("sub/model.bin", ["sub/*.bin"], [])
    assert not matches("model.bin", ["sub/*.bin"], [])
    assert matches("a/b/c.bin", ["*.bin"], [])


def test_a_single_star_does_not_cross_a_directory_separator() -> None:
    """``fnmatch`` let ``*`` cross ``/``, so ``sub/*.bin`` matched every level."""
    assert matches("sub/a.bin", ["sub/*.bin"], [])
    assert not matches("sub/x/a.bin", ["sub/*.bin"], [])
    assert matches("sub/x/a.bin", ["sub/**/*.bin"], [])
    assert matches("sub/a.bin", ["sub/**/*.bin"], [])  # ``**/`` may match zero dirs
    assert matches("a.bin", ["?.bin"], [])
    assert not matches("a/bb.bin", ["a/?.bin"], [])


def _ctx(params: dict[str, object]) -> RuntimeContext:
    return RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(core=CoreConfig()),
        params=params,
    )


def test_filters_reads_the_run_parameters() -> None:
    include, exclude = filters(_ctx({"include": ["*.bin"], "exclude": ["x.bin"]}))
    assert include == ["*.bin"]
    assert exclude == ["x.bin"]
    assert filters(_ctx({})) == ([], [])


# --- credentials ----------------------------------------------------------- #
def test_a_configured_option_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    found = credentials.resolve({"token": "from-config"}, "token", credentials.GITHUB_TOKEN_ENVS)
    assert found.value == "from-config"
    assert found.source == "config"
    assert found.origin == "token"


def test_the_environment_is_used_in_declared_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "first")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "second")
    found = credentials.resolve({}, "token", credentials.HF_TOKEN_ENVS)
    assert found.value == "first"
    assert found.source == "env"
    assert found.origin == "HF_TOKEN"


def test_an_empty_option_does_not_shadow_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-env")
    found = credentials.resolve({"token": ""}, "token", credentials.GITHUB_TOKEN_ENVS)
    assert found.value == "from-env"


def test_an_unset_credential_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*credentials.HF_TOKEN_ENVS, *credentials.GITHUB_TOKEN_ENVS):
        monkeypatch.delenv(name, raising=False)
    found = credentials.resolve({}, "token", credentials.HF_TOKEN_ENVS)
    assert found.value == ""
    assert found.source == ""
    assert not found


# --- digests --------------------------------------------------------------- #
def test_hash_file_matches_hashlib(tmp_path: Path) -> None:
    payload = b"downctl" * 1000
    target = tmp_path / "data.bin"
    target.write_bytes(payload)

    assert hash_file(str(target)) == hashlib.sha256(payload).hexdigest()
    assert hash_file(str(target), "md5") == hashlib.md5(payload).hexdigest()  # noqa: S324


def test_hash_file_reports_progress(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"x" * 4096)
    seen: list[int] = []
    hash_file(str(target), progress=seen.append)
    assert sum(seen) == 4096


def test_an_unknown_algorithm_is_rejected_not_guessed(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"x")
    assert supported("sha256")
    assert not supported("not-a-hash")
    with pytest.raises(ValueError):
        hash_file(str(target), "not-a-hash")
