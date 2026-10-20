"""Additional coverage for loader, rate limiter, probe and console commands."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from downctl import app
from downctl.core.ratelimit import RateLimiter
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

    # The user config directory is a second local-plugin location; isolate it so
    # the developer's own plugins can never run inside the test suite.
    xdg = tmp_path / "xdg"
    user_plugins = xdg / "downctl" / "plugins"
    user_plugins.mkdir(parents=True)
    (user_plugins / "userplug.py").write_text(
        "from downctl.core.task import DownloadTask\n"
        "from downctl.plugin.protocols import API_VERSION\n"
        "class U:\n"
        "    name='userx'\n"
        "    api_version=API_VERSION\n"
        "    priority=0\n"
        "    def can_handle(self, t): return t.startswith('userx://')\n"
        "    async def resolve(self, t, ctx): return [DownloadTask(url=t, target='x')]\n"
        "def register(registry):\n"
        "    registry.register('source', U(), origin='user')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(tmp_path)

    registry = discover(include_local=True)
    assert "localx" in registry.names("source")
    assert "userx" in registry.names("source")  # XDG_CONFIG_HOME is honoured
    assert registry.select_source("localx://thing").name == "localx"  # type: ignore[union-attr]


def test_local_plugin_discovery_never_reaches_a_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolating HOME/XDG must be enough to keep user plugin code from running.

    Without the isolation this test imports (and therefore executes) whatever
    the developer keeps in ``~/.config/downctl/plugins``.
    """
    home = tmp_path / "home"
    plugins = home / ".config" / "downctl" / "plugins"
    plugins.mkdir(parents=True)
    marker = tmp_path / "executed"
    (plugins / "evil.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    registry = discover(include_local=True)
    assert not marker.exists()
    assert "evil" not in registry.names("source")


# --- rate limiter ---------------------------------------------------------- #
async def test_rate_limiter_throttles() -> None:
    limiter = RateLimiter(10000.0)
    touched = []
    started = time.monotonic()
    await limiter.wait(2000, lambda: touched.append(1))
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15
    assert touched  # the heartbeat callback fired during the wait


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
    async def unreachable(
        session: object, target: str, *, proxy: str | None = None, policy: object = None
    ) -> dict[str, object]:
        return {
            "target": target,
            "status": None,
            "size": 0,
            "content_type": "",
            "final_url": target,
            "filename": "",
            "reachable": False,
        }

    monkeypatch.setattr(app, "probe_resource", unreachable)
    monkeypatch.setenv("DOWNCTL_CONFIG", "/nonexistent/config.toml")

    args = app._default_get_args()
    args.targets = ["http://127.0.0.1:1/nope"]
    assert await app._probe(args) == 1
    out = capsys.readouterr().out
    assert "unreachable" in out
