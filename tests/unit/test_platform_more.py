"""Extra coverage for platform helpers (WSL/Windows/pid/tty branches)."""

from __future__ import annotations

import sys

import pytest

from downctl import platform_utils as pu


def test_platform_kind_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu.platform, "system", lambda: "Darwin")
    assert pu.platform_kind() == "Darwin"
    monkeypatch.setattr(pu.platform, "system", lambda: "Windows")
    assert pu.platform_kind() == "Windows"
    monkeypatch.setattr(pu.platform, "system", lambda: "Plan9")
    assert pu.platform_kind() == "unknown"


def test_platform_kind_linux_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu.platform, "system", lambda: "Linux")

    def boom(*_a: object, **_k: object) -> object:
        raise OSError("no /proc")

    monkeypatch.setattr("builtins.open", boom)
    assert pu.platform_kind() == "Linux"


def test_stdout_stdin_is_tty_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stdin", None)
    assert pu.stdout_is_tty() is False
    assert pu.stdin_is_tty() is False


def test_pid_alive_self() -> None:
    import os

    assert pu.pid_alive(os.getpid()) is True


def test_pid_alive_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    if os.name == "nt":
        pytest.skip("POSIX signal path")

    def missing(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", missing)
    assert pu.pid_alive(424242) is False

    def denied(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert pu.pid_alive(1) is True

    def other(_pid: int, _sig: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "kill", other)
    assert pu.pid_alive(1) is False


def test_init_console_windows_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu.os, "name", "nt")
    pu.init_console()  # ctypes.windll is absent on Linux -> swallowed


def test_pid_alive_windows_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu.os, "name", "nt")
    assert pu.pid_alive(1) is False


def test_safe_io_reconfigures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Fake:
        encoding = "latin-1"

        def reconfigure(self, **_kw: object) -> None:
            calls.append("ok")

    monkeypatch.setattr(sys, "stdout", Fake())
    monkeypatch.setattr(sys, "stderr", Fake())
    pu.safe_io()
    assert calls == ["ok", "ok"]


def test_safe_io_no_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    pu.safe_io()  # no crash


def test_win_to_wsl_path_edges() -> None:
    assert pu.win_to_wsl_path("") == ""
    assert pu.win_to_wsl_path("relative/path") == "relative/path"


def test_detect_wsl_proxy_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pu.detect_wsl_proxy("http://p:1") == "http://p:1"


def test_detect_wsl_proxy_non_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "Linux")
    assert pu.detect_wsl_proxy("") == ""


def test_detect_wsl_proxy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "WSL")
    monkeypatch.setenv("WSL_HOST_IP", "10.0.0.5")
    assert pu.detect_wsl_proxy("") == "http://10.0.0.5:7897"


def test_detect_wsl_proxy_localhost_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "WSL")
    monkeypatch.setenv("WSL_HOST_IP", "127.0.0.1")
    assert pu.detect_wsl_proxy("") == ""


def test_detect_wsl_proxy_resolv_conf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "WSL")
    monkeypatch.delenv("WSL_HOST_IP", raising=False)

    class FakeFile:
        def __enter__(self) -> FakeFile:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(["# comment\n", "nameserver 172.20.0.1\n"])

    real_open = open

    def fake_open(path: str, *a: object, **k: object) -> object:
        if str(path) == "/etc/resolv.conf":
            return FakeFile()
        return real_open(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", fake_open)
    assert pu.detect_wsl_proxy("") == "http://172.20.0.1:7897"


def test_detect_wsl_proxy_resolv_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "WSL")
    monkeypatch.delenv("WSL_HOST_IP", raising=False)

    def boom(*_a: object, **_k: object) -> object:
        raise OSError

    monkeypatch.setattr("builtins.open", boom)
    assert pu.detect_wsl_proxy("") == ""


def test_detect_wsl_proxy_ipv6_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pu, "platform_kind", lambda: "WSL")
    monkeypatch.setenv("WSL_HOST_IP", "fe80::1")
    assert pu.detect_wsl_proxy("") == ""
