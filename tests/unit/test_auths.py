"""Tests for the built-in auth providers (env, command, netrc)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from downctl.builtin.auths import CommandAuth, EnvAuth, NetrcAuth
from downctl.builtin.auths.netrc_auth import _path
from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.credentials import AuthError
from downctl.plugin.protocols import AuthProvider

#: A value that must never appear in an error message. Real tokens are not
#: needed to prove the property; a marker makes a leak visible.
SECRET = "SECRET-VALUE"

_NETRC = """\
machine example.com login alice password s3cret
machine files.example.com login bob password hunter2
"""


def _ctx() -> RuntimeContext:
    # The providers never touch the context; it is part of the protocol.
    return RuntimeContext(session=None, config=Config())  # type: ignore[arg-type]


def _netrc_file(tmp_path: Path, content: str = _NETRC, *, mode: int = 0o600) -> Path:
    path = tmp_path / "netrc"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


# --------------------------------------------------------------------------- #
# env provider
# --------------------------------------------------------------------------- #
def test_providers_satisfy_the_auth_protocol() -> None:
    for provider in (EnvAuth(), CommandAuth(), NetrcAuth()):
        assert isinstance(provider, AuthProvider)
        assert provider.api_version
        assert provider.can_handle("example.com") is True


async def test_env_first_variable_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_TOKEN_A", "first")
    monkeypatch.setenv("DOWNCTL_TOKEN_B", "second")
    material = await EnvAuth().material(
        "example.com", _ctx(), {"env": ["DOWNCTL_TOKEN_A", "DOWNCTL_TOKEN_B"]}
    )
    assert material is not None
    assert material.headers == {"Authorization": "Bearer first"}
    assert material.source == "env DOWNCTL_TOKEN_A"


async def test_env_list_falls_through_to_a_set_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOWNCTL_TOKEN_A", raising=False)
    monkeypatch.setenv("DOWNCTL_TOKEN_B", "second")
    material = await EnvAuth().material(
        "example.com", _ctx(), {"env": ["DOWNCTL_TOKEN_A", "DOWNCTL_TOKEN_B"]}
    )
    assert material is not None
    assert material.headers == {"Authorization": "Bearer second"}
    assert material.source == "env DOWNCTL_TOKEN_B"


async def test_env_empty_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_TOKEN_A", "")
    monkeypatch.setenv("DOWNCTL_TOKEN_B", "second")
    material = await EnvAuth().material(
        "example.com", _ctx(), {"env": ["DOWNCTL_TOKEN_A", "DOWNCTL_TOKEN_B"]}
    )
    assert material is not None
    assert material.headers == {"Authorization": "Bearer second"}


async def test_env_missing_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOWNCTL_MISSING_TOKEN", raising=False)
    with pytest.raises(AuthError, match="DOWNCTL_MISSING_TOKEN is unset"):
        await EnvAuth().material("example.com", _ctx(), {"env": "DOWNCTL_MISSING_TOKEN"})


async def test_env_missing_list_names_all_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOWNCTL_TOKEN_A", raising=False)
    monkeypatch.delenv("DOWNCTL_TOKEN_B", raising=False)
    with pytest.raises(AuthError) as info:
        await EnvAuth().material(
            "example.com", _ctx(), {"env": ["DOWNCTL_TOKEN_A", "DOWNCTL_TOKEN_B"]}
        )
    message = str(info.value)
    assert "DOWNCTL_TOKEN_A" in message
    assert "DOWNCTL_TOKEN_B" in message


async def test_env_custom_header_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_TOKEN", "raw-token")
    material = await EnvAuth().material(
        "example.com",
        _ctx(),
        {"env": "DOWNCTL_TOKEN", "header": "X-Api-Key", "prefix": ""},
    )
    assert material is not None
    assert material.headers == {"X-Api-Key": "raw-token"}


async def test_env_unknown_option_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_TOKEN", "value")
    with pytest.raises(AuthError, match="env auth: unknown option 'environ'"):
        await EnvAuth().material("example.com", _ctx(), {"env": "DOWNCTL_TOKEN", "environ": "x"})


@pytest.mark.parametrize(
    "options",
    [
        {"env": True},
        {"env": []},
        {"env": ["DOWNCTL_TOKEN", 3]},
        {"env": "DOWNCTL_TOKEN", "header": 1},
        {"env": "DOWNCTL_TOKEN", "prefix": False},
        {},
    ],
)
async def test_env_rejects_invalid_options(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, object]
) -> None:
    monkeypatch.setenv("DOWNCTL_TOKEN", "value")
    with pytest.raises(AuthError, match="env auth"):
        await EnvAuth().material("example.com", _ctx(), options)


# --------------------------------------------------------------------------- #
# command provider
# --------------------------------------------------------------------------- #
async def test_command_success(monkeypatch: pytest.MonkeyPatch) -> None:
    material = await CommandAuth().material(
        "example.com", _ctx(), {"command": "printf token-from-command"}
    )
    assert material is not None
    assert material.headers == {"Authorization": "Bearer token-from-command"}
    # The source never repeats the command line: it may contain a token.
    assert material.source == "command"


async def test_command_custom_header_and_prefix() -> None:
    material = await CommandAuth().material(
        "example.com",
        _ctx(),
        {"command": "printf token", "header": "PRIVATE-TOKEN", "prefix": ""},
    )
    assert material is not None
    assert material.headers == {"PRIVATE-TOKEN": "token"}


async def test_command_nonzero_exit_is_an_error() -> None:
    with pytest.raises(AuthError, match="command exited 3"):
        await CommandAuth().material("example.com", _ctx(), {"command": "printf nope; exit 3"})


async def test_command_empty_output_is_an_error() -> None:
    with pytest.raises(AuthError, match="produced no output"):
        await CommandAuth().material("example.com", _ctx(), {"command": "true"})


async def test_command_multiline_output_is_an_error() -> None:
    with pytest.raises(AuthError, match="produced more than one line"):
        await CommandAuth().material("example.com", _ctx(), {"command": "printf 'one\\ntwo\\n'"})


async def test_command_timeout_kills_and_names_the_limit() -> None:
    with pytest.raises(AuthError, match="command timed out after 0.1s"):
        await CommandAuth().material("example.com", _ctx(), {"command": "sleep 5", "timeout": 0.1})


async def test_command_stderr_is_never_surfaced() -> None:
    with pytest.raises(AuthError, match="command exited 7") as info:
        await CommandAuth().material(
            "example.com",
            _ctx(),
            {"command": f"printf {SECRET} >&2; exit 7"},
        )
    assert SECRET not in str(info.value)


async def test_command_error_never_contains_the_value() -> None:
    with pytest.raises(AuthError) as info:
        await CommandAuth().material(
            "example.com",
            _ctx(),
            {"command": f"printf '{SECRET}\\n{SECRET}\\n'"},
        )
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"command": 1},
        {"command": "printf x", "timeout": True},
        {"command": "printf x", "timeout": 0},
        {"command": "printf x", "timeout": -1},
        {"command": "printf x", "timeout": "10"},
        {"command": "printf x", "header": 1},
        {"command": "printf x", "prefix": False},
        {"command": "printf x", "mode": "x"},
    ],
)
async def test_command_rejects_invalid_options(options: dict[str, object]) -> None:
    with pytest.raises(AuthError, match="command auth"):
        await CommandAuth().material("example.com", _ctx(), options)


# --------------------------------------------------------------------------- #
# netrc provider
# --------------------------------------------------------------------------- #
async def test_netrc_success(tmp_path: Path) -> None:
    path = _netrc_file(tmp_path)
    material = await NetrcAuth().material("example.com", _ctx(), {"path": str(path)})
    assert material is not None
    assert material.basic == ("alice", "s3cret")
    assert material.headers == {}
    assert material.source == f"netrc {path}"


async def test_netrc_missing_machine_returns_none(tmp_path: Path) -> None:
    path = _netrc_file(tmp_path)
    assert await NetrcAuth().material("unknown.example", _ctx(), {"path": str(path)}) is None


async def test_netrc_missing_file_returns_none(tmp_path: Path) -> None:
    assert (
        await NetrcAuth().material("example.com", _ctx(), {"path": str(tmp_path / "absent")})
        is None
    )


async def test_netrc_unknown_option_is_refused(tmp_path: Path) -> None:
    path = _netrc_file(tmp_path)
    with pytest.raises(AuthError, match="netrc auth: unknown option 'mode'"):
        await NetrcAuth().material("example.com", _ctx(), {"path": str(path), "mode": "x"})


@pytest.mark.parametrize("path", [1, True, ""])
async def test_netrc_rejects_an_invalid_path(path: object) -> None:
    with pytest.raises(AuthError, match="netrc auth"):
        await NetrcAuth().material("example.com", _ctx(), {"path": path})


@pytest.mark.skipif(os.name != "posix", reason="the warning is POSIX-only")
async def test_netrc_warns_when_readable_by_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _netrc_file(tmp_path, mode=0o644)
    await NetrcAuth().material("example.com", _ctx(), {"path": str(path)})
    stderr = capsys.readouterr().err
    assert "readable by others" in stderr
    assert "s3cret" not in stderr


async def test_netrc_parse_error_names_the_file(tmp_path: Path) -> None:
    path = _netrc_file(tmp_path, "this is not netrc\n")
    with pytest.raises(AuthError, match="netrc auth") as info:
        await NetrcAuth().material("example.com", _ctx(), {"path": str(path)})
    assert str(path) in str(info.value)


def test_netrc_default_path_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # ``expanduser`` reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert _path({}) == str(tmp_path / ".netrc")
    assert _path({"path": "~/credentials"}) == str(tmp_path / "credentials")
