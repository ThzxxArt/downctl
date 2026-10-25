"""The CLI wires ``--progress`` to the reporter, not just the parser."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from downctl import app, console


@pytest.fixture(autouse=True)
def _clean_console() -> None:
    console.set_level(console.NORMAL)
    console.set_color(None)
    console.set_machine_readable(False)
    yield
    console.reset_live()
    console.set_level(console.NORMAL)
    console.set_color(None)
    console.set_machine_readable(False)


def _args(server: object, destination: Path, **overrides: object) -> argparse.Namespace:
    args = app._default_get_args()
    args.targets = [server.url("file")]  # type: ignore[attr-defined]
    args.dir = str(destination)
    args.connections = 2
    args.retries = 1
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


@pytest.mark.asyncio
async def test_progress_plain_writes_periodic_lines_to_stderr(
    file_server: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(file_server, tmp_path / "plain", progress="plain")
    code = await app._get(args)
    captured = capsys.readouterr()
    assert code == 0
    assert "downctl:" in captured.err
    assert "done:" in captured.out


@pytest.mark.asyncio
async def test_progress_off_writes_no_progress_lines(
    file_server: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(file_server, tmp_path / "off", progress="off")
    code = await app._get(args)
    captured = capsys.readouterr()
    assert code == 0
    assert "downctl:" not in captured.err
    assert "done:" in captured.out


@pytest.mark.asyncio
async def test_explicit_json_reporter_still_owns_stdout(
    file_server: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _args(file_server, tmp_path / "json", reporter="json", progress="live")
    code = await app._get(args)
    captured = capsys.readouterr()
    assert code == 0
    assert "done:" not in captured.out
    assert '"event": "run_finish"' in captured.out
