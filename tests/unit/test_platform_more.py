"""Extra coverage for platform helpers (WSL/Windows/pid/tty branches)."""

from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace
from typing import Any

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
    # ``ctypes.windll`` does not exist off Windows; the failure is swallowed so
    # importing downctl on a non-console host still works.
    assert pu.init_console() is None


class _Fn:
    """A callable that also accepts the ``restype``/``argtypes`` assignment.

    The production code configures the prototypes the way ctypes expects, so a
    stand-in cannot be a bound method -- those reject attribute assignment.
    """

    def __init__(self, impl: object) -> None:
        self._impl = impl

    def __call__(self, *args: object) -> object:
        return self._impl(*args)  # type: ignore[operator]


class _FakeKernel32:
    """Stand-in for ``ctypes.windll.kernel32`` that records what it was asked."""

    def __init__(
        self,
        *,
        handle: int = 1234,
        exit_code: int = 0,
        exit_ok: bool = True,
        exit_raises: bool = False,
    ) -> None:
        self.handle = handle
        self.exit_code = exit_code
        self.exit_ok = exit_ok
        self.exit_raises = exit_raises
        self.opened: list[tuple[int, bool, int]] = []
        self.closed: list[int] = []
        self.OpenProcess = _Fn(self._open_process)
        self.GetExitCodeProcess = _Fn(self._get_exit_code)
        self.CloseHandle = _Fn(self._close_handle)

    def _open_process(self, access: int, inherit: bool, pid: int) -> int:
        self.opened.append((access, inherit, pid))
        return self.handle

    def _get_exit_code(self, handle: int, out: Any) -> int:
        if self.exit_raises:
            raise OSError("the query itself failed")
        if not self.exit_ok:
            return 0
        ctypes.cast(out, ctypes.POINTER(ctypes.c_uint32)).contents.value = self.exit_code
        return 1

    def _close_handle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1


def _fake_windows(monkeypatch: pytest.MonkeyPatch, kernel32: _FakeKernel32) -> None:
    monkeypatch.setattr(pu.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)


def test_pid_alive_windows_reports_a_killed_process_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exited process is still *openable*, so opening it is not proof of life.

    Windows keeps the process object -- and the pid -- alive until the last
    handle to it closes, which is exactly the state a killed downloader leaves
    behind. Reporting that as "alive" makes its lock unbreakable and breaks
    "kill it and re-run".
    """
    kernel32 = _FakeKernel32(handle=4242, exit_code=1)  # opened, but terminated
    _fake_windows(monkeypatch, kernel32)
    assert pu.pid_alive(999999) is False
    assert kernel32.closed == [4242], "the handle must not leak"


def test_pid_alive_windows_reports_a_running_process_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32(handle=7, exit_code=pu._STILL_ACTIVE)
    _fake_windows(monkeypatch, kernel32)
    assert pu.pid_alive(4242) is True
    assert kernel32.opened == [(pu._QUERY_LIMITED, False, 4242)]
    assert kernel32.closed == [7]


def test_pid_alive_windows_without_a_handle_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = _FakeKernel32(handle=0)
    _fake_windows(monkeypatch, kernel32)
    assert pu.pid_alive(4242) is False
    assert kernel32.closed == [], "nothing to close when the open failed"


def test_pid_alive_windows_when_the_exit_code_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cannot tell: keep the lock rather than steal one that may be live."""
    kernel32 = _FakeKernel32(handle=9, exit_ok=False)
    _fake_windows(monkeypatch, kernel32)
    assert pu.pid_alive(4242) is True
    assert kernel32.closed == [9]


def test_pid_alive_windows_when_the_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing query is also "cannot tell", and still must not leak the handle."""
    kernel32 = _FakeKernel32(handle=11, exit_raises=True)
    _fake_windows(monkeypatch, kernel32)
    assert pu.pid_alive(4242) is True
    assert kernel32.closed == [11]


def test_pid_alive_windows_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows there is no ``windll`` at all."""
    monkeypatch.setattr(pu.os, "name", "nt")
    monkeypatch.delattr(ctypes, "windll", raising=False)
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
    # A windowed interpreter has no stdio; reconfiguring it must not crash.
    assert pu.safe_io() is None


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
