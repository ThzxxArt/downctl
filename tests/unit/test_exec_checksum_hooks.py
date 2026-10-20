"""Tests for the exec and checksum hook plugins."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from downctl.builtin.hooks.checksum_hook import ChecksumHook
from downctl.builtin.hooks.exec_hook import ExecHook
from downctl.core.task import DownloadTask


def _task(path: Path, target: str = "a.bin") -> DownloadTask:
    task = DownloadTask(url="https://h/a.bin", target=target)
    task.meta["path"] = str(path)
    return task


# --- checksum hook -------------------------------------------------------- #
def _finished(
    path: Path, *, witness: str = "validator", target: str = "a.bin"
) -> dict[str, object]:
    """An ``on_task_finish`` payload as the engine emits it."""
    return {"task": _task(path, target), "ok": True, "witness": witness}


def test_checksum_hook_writes_sidecar(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", _finished(f))
    sidecar = tmp_path / "a.bin.sha256"
    assert sidecar.is_file()
    assert hashlib.sha256(b"abc").hexdigest() in sidecar.read_text(encoding="utf-8")


def test_checksum_hook_does_not_vouch_for_an_unverified_completion(tmp_path: Path) -> None:
    """A sidecar is consumed by ``--checksums`` as "the remote had these bytes".

    An unversioned skip (``witness == "none"``) never compared anything, so it
    has no such claim to make and must not leave an artifact behind.
    """
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", _finished(f, witness="none"))
    assert not (tmp_path / "a.bin.sha256").exists()


def test_checksum_hook_trusts_a_digest_bound_completion(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", _finished(f, witness="checksum"))
    assert (tmp_path / "a.bin.sha256").is_file()


def test_checksum_hook_ignores_failure(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", {"task": _task(f), "ok": False})
    assert not (tmp_path / "a.bin.sha256").exists()


def test_checksum_hook_falls_back_to_sha256(tmp_path: Path) -> None:
    assert ChecksumHook(algo="nonsense").algo == "sha256"
    assert ChecksumHook(algo="sha512").algo == "sha512"


def test_configure_rejects_an_unsupported_algorithm(tmp_path: Path) -> None:
    """A rejected value must leave the hook on its working default."""
    hook = ChecksumHook()
    problems = hook.configure({"algo": "not-a-hash"})
    assert len(problems) == 1
    assert "algo must be one of" in problems[0]
    assert hook.algo == "sha256"


def test_configure_accepts_a_supported_algorithm() -> None:
    hook = ChecksumHook()
    assert hook.configure({"algo": "SHA512"}) == []
    assert hook.algo == "sha512"


def test_checksum_hook_missing_path(tmp_path: Path) -> None:
    task = DownloadTask(url="u", target="a.bin")  # no meta["path"]
    ChecksumHook().on_event("on_task_finish", {"task": task, "ok": True, "witness": "validator"})
    assert list(tmp_path.iterdir()) == []


def test_checksum_hook_missing_file(tmp_path: Path) -> None:
    ChecksumHook().on_event("on_task_finish", _finished(tmp_path / "nope.bin"))
    assert not (tmp_path / "nope.bin.sha256").exists()


# --- exec hook ------------------------------------------------------------ #
def test_exec_hook_runs_command(tmp_path: Path) -> None:
    import sys

    script = tmp_path / "rec.py"
    script.write_text(
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['DOWNCTL_TARGET'])",
        encoding="utf-8",
    )
    marker = tmp_path / "out.txt"
    hook = ExecHook(command=f'"{sys.executable}" "{script}" "{marker}"')
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert marker.read_text(encoding="utf-8") == "a.bin"


def test_exec_hook_no_command_is_noop(tmp_path: Path) -> None:
    marker = tmp_path / "out.txt"
    ExecHook(command="").on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert not marker.exists()


def test_exec_hook_keeps_json_stdout_clean(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    import sys

    from downctl import console

    script = tmp_path / "hello.py"
    script.write_text("print('HOOK-OUTPUT')\n", encoding="utf-8")
    hook = ExecHook(command=f'"{sys.executable}" "{script}"')
    console.set_machine_readable(True)
    try:
        hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    finally:
        console.set_machine_readable(False)
    out, err = capfd.readouterr()
    assert "HOOK-OUTPUT" not in out
    assert "HOOK-OUTPUT" in err


def test_exec_hook_redacts_presigned_urls(tmp_path: Path) -> None:
    import sys

    script = tmp_path / "dump.py"
    script.write_text(
        "import os, sys; open(sys.argv[1], 'w').write("
        "os.environ['DOWNCTL_URL'] + '|' + os.environ['DOWNCTL_ORIGIN'])",
        encoding="utf-8",
    )
    marker = tmp_path / "out.txt"
    task = DownloadTask(
        url="http://s3/b?X-Amz-Signature=SECRET",
        target="b",
        origin="s3://bucket/b",
        meta={"redact_url": True, "path": str(tmp_path / "b")},
    )
    hook = ExecHook(command=f'"{sys.executable}" "{script}" "{marker}"')
    hook.on_event("on_task_finish", {"task": task, "ok": True})
    text = marker.read_text(encoding="utf-8")
    assert "SECRET" not in text
    assert "http://s3/b" in text
    assert "s3://bucket/b" in text


def test_exec_hook_ignores_unselected_event(tmp_path: Path) -> None:
    marker = tmp_path / "out.txt"
    hook = ExecHook(command=f'touch "{marker}"', events=["on_finish"])
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert not marker.exists()


def test_exec_hook_reports_a_command_that_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook that cannot run must warn and never break the download."""
    from downctl.builtin.hooks import exec_hook as module

    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("exec format error")

    monkeypatch.setattr(module.subprocess, "run", boom)
    hook = ExecHook(command="whatever")
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert "command failed" in capsys.readouterr().err


def test_exec_hook_reports_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing command is a warning, not a crash.

    The status itself is the shell's business -- POSIX ``sh`` reports 127 for a
    missing command, ``cmd.exe`` something else -- so only "nonzero, and it was
    reported" is asserted here.
    """
    hook = ExecHook(command="this-command-does-not-exist-xyz")
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    err = capsys.readouterr().err
    match = re.search(r"exited with (\d+)", err)
    assert match, f"the hook must report the shell's exit status: {err!r}"
    assert int(match.group(1)) != 0
