"""The dry-run ``witness`` label, and the quiet reporter's silence.

``app._witness_label`` is what a machine-readable dry run reports next to
``done``: it has to distinguish a version that this run actually compared from
a marker that merely carries one. These tests drive the function directly (the
real caller feeds it ``done`` computed by ``_completed_bytes``) so every return
path is exercised without standing up a server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from downctl import app
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.core import pieces as pc
from downctl.core.task import DownloadTask
from downctl.urls import url_identity

URL = "http://127.0.0.1:9/object.bin"
IDENTITY = url_identity(URL)


def _target(tmp_path: Path, payload: bytes = b"0123456789") -> str:
    path = tmp_path / "object.bin"
    path.write_bytes(payload)
    return str(path)


def _task(**meta: object) -> DownloadTask:
    task = DownloadTask(url=URL, target="object.bin")
    task.meta.update(meta)
    return task


def _write_marker(target: str, *, validator: str = "", checksum_hex: str = "") -> None:
    assert pc.save_completion(
        target,
        IDENTITY,
        "sha256" if checksum_hex else "",
        checksum_hex,
        validator=validator,
    )


def _write_ledger(target: str, *, validator: str) -> None:
    assert pc.save(
        target,
        pc.Bitmap(
            size=10,
            pieces=1,
            bits=[1],
            min_size=10,
            piece_size=pc.MIN_PIECE_SIZE,
            url_sha256=IDENTITY,
            validator=validator,
        ),
    )


# --------------------------------------------------------------------------- #
# completion markers
# --------------------------------------------------------------------------- #
def test_nothing_counted_as_done_is_never_a_proof(tmp_path: Path) -> None:
    """``done <= 0`` wins even when a marker happens to be present."""
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"')

    assert app._witness_label(_task(validator='"v1"'), target, 0) == "none"
    assert app._witness_label(_task(validator='"v1"'), target, -1) == "none"


def test_a_marker_carrying_a_witness_this_run_could_not_compare(
    tmp_path: Path,
) -> None:
    """A marker read without a probe falls back to the proof it *does* carry.

    ``observed`` is ``None`` here, which is what a local/SFTP transport or a
    probe-less skip produces. The recorded ETag must not be presented as a
    comparison, but the digest it also recorded is still a real claim.
    """
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"', checksum_hex="a" * 64)

    assert app._witness_label(_task(), target, 10) == "checksum"


def test_a_marker_carrying_only_a_witness_this_run_could_not_compare(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"')

    assert app._witness_label(_task(), target, 10) == "none"


def test_an_observed_witness_matching_the_marker_is_a_validator(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"')

    assert app._witness_label(_task(validator='"v1"'), target, 10) == "validator"


def test_the_server_naming_no_witness_is_not_a_comparison(tmp_path: Path) -> None:
    """An empty observed witness is a real observation, but nothing to compare."""
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"', checksum_hex="b" * 64)

    assert app._witness_label(_task(validator=""), target, 10) == "checksum"


def test_a_replaced_object_is_reported_unproven(tmp_path: Path) -> None:
    """The real call path refuses a witness mismatch before it labels it.

    ``done`` is the output of ``_completed_bytes``, whose trust predicate
    (``pieces.completion_valid``) compares a recorded witness with the observed
    one and refuses a mismatch. A replaced object therefore reaches
    ``_witness_label`` as ``done=0``; calling it with ``done > 0`` and a
    mismatched marker would be outside the contract it documents, because it
    labels a completion the caller has already proven.
    """
    target = _target(tmp_path)
    _write_marker(target, validator='"v1"')

    assert not pc.completion_valid(target, 10, IDENTITY, validator='"v2"')
    assert app._witness_label(_task(validator='"v2"'), target, 0) == "none"


def test_a_marker_without_a_version_falls_back_to_its_digest(tmp_path: Path) -> None:
    target = _target(tmp_path)
    _write_marker(target, checksum_hex="c" * 64)

    assert app._witness_label(_task(validator='"v1"'), target, 10) == "checksum"


# --------------------------------------------------------------------------- #
# resume ledgers
# --------------------------------------------------------------------------- #
def test_a_ledger_witness_is_a_validator_only_when_this_run_saw_the_object(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    _write_ledger(target, validator='"v1"')

    assert app._witness_label(_task(validator='"v1"'), target, 10) == "validator"
    assert app._witness_label(_task(), target, 10) == "none"


def test_no_state_at_all_is_unversioned(tmp_path: Path) -> None:
    target = _target(tmp_path)

    assert app._witness_label(_task(validator='"v1"'), target, 10) == "none"


# --------------------------------------------------------------------------- #
# the quiet reporter prints nothing, on every hook it implements
# --------------------------------------------------------------------------- #
def test_quiet_reporter_is_silent_for_the_task_hooks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = QuietReporter()
    task = DownloadTask(url=URL, target="object.bin", size=10)

    reporter.task_started(task)
    reporter.task_progress(task, 5)
    reporter.task_finished(task, True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_quiet_reporter_leaves_the_optional_run_hooks_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_started``/``run_finished`` are optional reporter extensions.

    The engine feature-detects them with ``getattr``, so a reporter that does
    not implement them emits nothing at those points. Asserting the absence is
    the behavioural claim: if the quiet reporter grew a hook that printed, this
    would fail.
    """
    reporter = QuietReporter()

    hooks = {name: getattr(reporter, name, None) for name in ("run_started", "run_finished")}
    assert hooks == {"run_started": None, "run_finished": None}

    # Emulate the engine's dispatch: only a present hook is called.
    for name, args in (("run_started", (1, 10)), ("run_finished", (1, 0))):
        hook = hooks[name]
        if callable(hook):
            hook(*args)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
