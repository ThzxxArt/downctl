"""CLI honesty: machine output, opt-in local plugins and error classification."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import aiohttp
import pytest

from downctl import app, console
from downctl.core import pieces as pc


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)
    console.set_level(console.NORMAL)
    yield
    console.set_machine_readable(False)
    console.set_level(console.NORMAL)


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def test_status_json_reports_incomplete_and_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = str(tmp_path / "a.bin")
    Path(target).write_bytes(b"x" * 10)
    pc.save(target, pc.Bitmap(10, 3, [1, 0, 0], 10))
    (tmp_path / ".downctl.failed").write_text(
        "url\thttps://h/f\tf.bin\t\t\t0\turl\n", encoding="utf-8"
    )

    assert app.cmd_status(argparse.Namespace(path=str(tmp_path), json=True)) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["incomplete"][0]["target"] == "a.bin"
    assert record["incomplete"][0]["percent"] == 33
    assert record["failed"] == ["f.bin"]


def test_clean_json_dry_run_keeps_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")
    args = argparse.Namespace(path=str(tmp_path), dry_run=True, json=True)
    assert app.cmd_clean(args) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["dry_run"] is True
    assert record["count"] == 1
    assert (tmp_path / ".downctl.failed").exists()


def test_clean_json_removes_when_not_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --json changes the output format, not the command semantics: without
    # --dry-run the artifacts must actually be removed.
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")
    args = argparse.Namespace(path=str(tmp_path), dry_run=False, json=True)
    assert app.cmd_clean(args) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["dry_run"] is False
    assert record["count"] == 1
    assert not (tmp_path / ".downctl.failed").exists()


def test_status_json_stays_pure_with_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    console.set_level(console.VERBOSE)
    try:
        assert app.cmd_status(argparse.Namespace(path=str(tmp_path), json=True)) == 0
    finally:
        console.set_level(console.NORMAL)
    record = json.loads(capsys.readouterr().out)
    assert record["directory"] == str(tmp_path)
    assert record["incomplete"] == []


def test_status_masks_a_legacy_failed_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".downctl.failed").write_text("https://h/f?token=SUPERSECRET\n", encoding="utf-8")
    assert app.cmd_status(argparse.Namespace(path=str(tmp_path), json=True)) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["failed"] == ["https://h/f?token=***"]


def test_clean_json_stays_pure_with_verbose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    console.set_level(console.VERBOSE)
    try:
        assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=True, json=True)) == 0
    finally:
        console.set_level(console.NORMAL)
    record = json.loads(capsys.readouterr().out)
    assert record["directory"] == str(tmp_path)
    assert record["items"] == []


def test_plugin_local_directory_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = tmp_path / "downctl_plugins"
    plugins.mkdir()
    marker = tmp_path / "executed"
    (plugins / "reg.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('loaded')\n"
        "def register(registry):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # The user plugin directory is a second local-plugin location: isolate it so
    # the developer's own plugins can never run inside the test suite.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert app.cmd_plugin(argparse.Namespace(plugin_action="list")) == 0
    assert not marker.exists()  # local code never runs unasked

    assert app.cmd_plugin(argparse.Namespace(plugin_action="list", local_plugins=True)) == 0
    assert marker.read_text(encoding="utf-8") == "loaded"
    capsys.readouterr()


def test_config_show_masks_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[core]\nmirror = { "http://user:pass@h/" = "http://m/" }\n'
        '[plugins.huggingface]\ntoken = "hf_SUPERSECRET"\n'
        '[plugins.sftp]\npassword = "P@ssw0rd"\n'
        '[plugins.webhook]\nurl = "https://h/hook?token=WEBHOOKSECRET"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))

    assert app.cmd_config(argparse.Namespace(config_action="show")) == 0
    out = capsys.readouterr().out
    assert "hf_SUPERSECRET" not in out
    assert "P@ssw0rd" not in out
    assert "user:pass" not in out
    assert "WEBHOOKSECRET" not in out
    assert "http://h/" in out
    assert "***" in out


def test_guard_redacts_url_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> int:
        raise aiohttp.InvalidURL("http://h/x?token=SUPERSECRET")

    assert app._guard(boom) == 1
    err = capsys.readouterr().err
    assert "SUPERSECRET" not in err
    assert "token=***" in err


def test_guard_reports_local_io_errors(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> int:
        raise OSError("permission denied")

    assert app._guard(boom) == 1
    assert "local I/O error" in capsys.readouterr().err


def test_guard_reports_network_errors(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> int:
        raise aiohttp.ClientConnectionError("connection reset")

    assert app._guard(boom) == 1
    assert "network error" in capsys.readouterr().err


def test_wsl_windows_paths_are_translated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "platform_kind", lambda: "WSL")
    cfg = app._build_config(make_args(dir="D:\\models"))
    assert cfg.core.dir == "/mnt/d/models"


async def test_probe_rejects_non_http_targets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    args = app._default_get_args()
    args.targets = ["hf://org/repo"]
    assert await app._probe(args) == 1
    out = capsys.readouterr().out
    assert "unsupported" in out


async def test_probe_masks_tokens_in_query_strings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def unreachable(
        session: object, target: str, *, proxy: str | None = None
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
    args = app._default_get_args()
    args.targets = ["http://127.0.0.1:1/x?token=SUPERSECRET"]
    args.json = True
    assert await app._probe(args) == 1
    out = capsys.readouterr().out
    assert "SUPERSECRET" not in out
    assert "token=" in out


async def test_probe_treats_http_errors_as_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def not_found(
        session: object, target: str, *, proxy: str | None = None
    ) -> dict[str, object]:
        return {
            "target": target,
            "status": 404,
            "size": 0,
            "content_type": "text/plain",
            "final_url": target,
            "filename": "missing",
            "reachable": True,
        }

    monkeypatch.setattr(app, "probe_resource", not_found)
    args = app._default_get_args()
    args.targets = ["https://h/missing"]
    assert await app._probe(args) == 1
    assert "status=404" in capsys.readouterr().out
