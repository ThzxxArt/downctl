"""Additional coverage for loader, rate limiter, sink, probe and console commands."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from downctl import app
from downctl.builtin.sinks.filesystem_sink import FilesystemSink
from downctl.core.ratelimit import RateLimiter
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover


# --- local plugin loading -------------------------------------------------- #
def test_load_local_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugins = tmp_path / "downctl_plugins"
    plugins.mkdir()
    (plugins / "reg.py").write_text(
        "from downctl.core.task import DownloadTask\n"
        "from downctl.plugin.protocols import API_VERSION\n"
        "class S:\n"
        "    name='localx'\n"
        "    api_version=API_VERSION\n"
        "    priority=0\n"
        "    def can_handle(self, t): return t.startswith('localx://')\n"
        "    async def resolve(self, t, ctx): return [DownloadTask(url=t, target='x')]\n"
        "def register(registry):\n"
        "    registry.register('source', S(), origin='reg')\n",
        encoding="utf-8",
    )
    (plugins / "broken.py").write_text("this is not python\n", encoding="utf-8")
    (plugins / "reject.py").write_text(
        "def register(registry):\n    raise RuntimeError('nope')\n", encoding="utf-8"
    )
    (plugins / "_ignored.py").write_text("raise RuntimeError('never loaded')\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    registry = discover(include_local=True)
    assert "localx" in registry.names("source")
    assert registry.select_source("localx://thing").name == "localx"  # type: ignore[union-attr]


# --- rate limiter ---------------------------------------------------------- #
async def test_rate_limiter_throttles() -> None:
    limiter = RateLimiter(1000.0)
    touched = []
    started = time.monotonic()
    await limiter.wait(4000, lambda: touched.append(1))
    elapsed = time.monotonic() - started
    assert elapsed > 0
    assert touched  # the heartbeat callback fired during the wait


# --- filesystem sink ------------------------------------------------------- #
async def test_filesystem_sink_writes(tmp_path: Path) -> None:
    sink = FilesystemSink()
    target = tmp_path / "sub" / "a.bin"
    task = DownloadTask(url="u", target="a.bin")
    task.meta["path"] = str(target)
    await sink.open(task, None)  # type: ignore[arg-type]
    await sink.write(0, b"hello ")
    await sink.write(6, b"world")
    await sink.close(success=True)
    assert target.read_bytes() == b"hello world"


async def test_filesystem_sink_close_without_open() -> None:
    await FilesystemSink().close(success=False)


async def test_filesystem_sink_write_without_open() -> None:
    with pytest.raises(RuntimeError):
        await FilesystemSink().write(0, b"x")


# --- console commands ------------------------------------------------------ #
def test_cmd_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.cmd_doctor(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "downctl" in out
    assert "plugins" in out


def test_wizard_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "stdin_is_tty", lambda: False)
    assert app.cmd_wizard(argparse.Namespace()) == 1


def test_wizard_runs_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "stdin_is_tty", lambda: True)
    answers = iter(["http://h/a.bin http://h/b.bin", ""])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    captured: dict[str, object] = {}

    def fake_get(namespace: argparse.Namespace) -> int:
        captured["targets"] = namespace.targets
        captured["dir"] = namespace.dir
        return 0

    monkeypatch.setattr(app, "cmd_get", fake_get)
    assert app.cmd_wizard(argparse.Namespace()) == 0
    assert captured["targets"] == ["http://h/a.bin", "http://h/b.bin"]


def test_wizard_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "stdin_is_tty", lambda: True)

    def boom(*_a: object) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert app.cmd_wizard(argparse.Namespace()) == 1


async def test_probe_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    monkeypatch.setenv("DOWNCTL_CONFIG", "/nonexistent/config.toml")

    args = app._default_get_args()
    args.targets = ["http://127.0.0.1:1/nope"]
    assert await app._probe(args) == 0
    out = capsys.readouterr().out
    assert "unreachable" in out
