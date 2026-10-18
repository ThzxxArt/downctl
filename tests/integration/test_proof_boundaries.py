"""Regression tests for the proof boundaries the 0.1.4 review broke through.

Each test here reproduces a defect the first 0.1.4 release shipped, and each one
asserts the *claim* the code makes rather than merely that a download finished.
The shared theme: a claim must rest on something the run actually checked.

- a completion marker may only name a version this run obtained or compared;
- a file is not destroyed before its replacement exists;
- the response's own validator is the only proof that all the pieces come from
  one revision, because ``If-Range`` is a request a server may ignore;
- a failure is classified *before* it is reported, or the machine-readable
  record cannot say whether re-running is worth anything.

The tasks here set ``meta['validator']``/``meta['if_range']`` directly: that is
exactly what ``app._probe_task`` records, and doing it here keeps each test
about the transfer and engine boundary instead of about the probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from downctl import app, console
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.config import Config, CoreConfig
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.engine import _recorded_proof, download_task
from downctl.core.errors import FAILURE_TRANSIENT_EXHAUSTED
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover
from downctl.urls import url_identity

PIECE = 1 << 20
URL = "http://h/model.bin"


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)


def make_ctx(session: aiohttp.ClientSession, tmp_path: Path, **core: object) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path), **core)),  # type: ignore[arg-type]
        registry=discover(),
    )


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class RecordingReporter(QuietReporter):
    """Keeps the task state it saw at ``task_finished`` time.

    The state is read *then*, not afterwards, because the defect was an ordering
    one: whatever the engine wrote later could not repair a record that had
    already been emitted.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[bool, object, object]] = []

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        self.seen.append((ok, task.meta.get("failure"), task.meta.get("witness")))


def task_for(server, name: str, *, validator: str | None = None) -> DownloadTask:
    """A task as ``app._probe_task`` would leave it for ``server``/``name``."""
    url = server.url(name)
    task = DownloadTask(url=url, target=name, size=len(server.data))
    if validator is not None:
        task.meta["validator"] = validator
        task.meta["if_range"] = validator
    return task


async def run(server, session, tmp_path: Path, task: DownloadTask, **kwargs: object):
    reporter = RecordingReporter()
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=reporter,
        stall_limit=5,
        **kwargs,  # type: ignore[arg-type]
    )
    return ok, reporter


# --------------------------------------------------------------------------- #
# a completion marker may not name a version this run never compared
# --------------------------------------------------------------------------- #
async def test_a_carried_over_completion_is_not_certified_with_the_new_witness(
    file_server, session, tmp_path: Path
) -> None:
    """The bytes were already there; this run compared nothing, so it claims nothing.

    Reproduced before the fix: a complete file whose marker recorded no witness
    (every 0.1.3 download, and any server without ``ETag``) was handed back by
    ``resolve`` with an all-ones grid — not one request was made — and the
    engine then stamped the freshly probed witness onto it. From then on
    ``--skip-existing --require-witness`` reported the stale file as
    ``witness: validator``: the strictest flag in the tool signing a certificate
    for content the run never looked at.
    """
    data = file_server.data
    target = tmp_path / "versioned"
    target.write_bytes(data)
    # A 0.1.3 marker: bound to the URL and the bytes, but to no version.
    assert pc.save_completion(str(target), url_identity(file_server.url("versioned")))

    file_server.state.replacements[2] = data  # same size, new revision
    file_server.state.version = 2
    task = task_for(file_server, "versioned", validator='"v2"')
    ok, reporter = await run(file_server, session, tmp_path, task)

    assert ok
    assert file_server.state.ranges == [], "a carried-over completion fetches nothing"
    record = pc.load_completion(str(target))
    assert record is not None
    assert record["validator"] == "", "a witness this run never compared was stamped"
    assert reporter.seen[-1][2] != "validator"
    assert task.meta.get("witness") != "validator"
    # The bytes are still the revision the marker described: nothing was
    # fetched, so nothing could have changed them.
    assert sha256(target.read_bytes()) == sha256(data)


async def test_require_witness_refuses_the_carried_over_completion(
    file_server, session, tmp_path: Path
) -> None:
    """With ``--require-witness`` the same file is fetched instead of blessed."""
    data = file_server.data
    target = tmp_path / "versioned"
    target.write_bytes(data)
    assert pc.save_completion(str(target), url_identity(file_server.url("versioned")))

    file_server.state.replacements[2] = bytes(byte ^ 0xFF for byte in data)
    file_server.state.version = 2
    task = task_for(file_server, "versioned", validator='"v2"')
    task.meta["require_witness"] = True
    ok, _ = await run(file_server, session, tmp_path, task)

    assert ok
    assert target.read_bytes() == file_server.state.current
    record = pc.load_completion(str(target))
    assert record is not None and record["validator"] == '"v2"'


async def test_a_206_without_a_validator_does_not_earn_a_version_claim(
    file_server, session, tmp_path: Path
) -> None:
    """A server that names the version on HEAD but not on the body response.

    The bytes are real and the window is exact, but nothing ties them to a
    revision, so the marker must not claim one. Trusting the HEAD's witness
    instead is precisely the unchecked claim this release removes.
    """
    task = task_for(file_server, "done-without-validator", validator='"v1"')
    ok, reporter = await run(file_server, session, tmp_path, task)

    assert ok
    record = pc.load_completion(str(tmp_path / "done-without-validator"))
    assert record is not None
    assert record["validator"] == ""
    assert task.meta.get("witness") == "none"
    assert reporter.seen[-1][2] == "none"


# --------------------------------------------------------------------------- #
# a finished file is not destroyed before its replacement exists
# --------------------------------------------------------------------------- #
async def test_a_finished_file_survives_a_replacement_that_cannot_be_fetched(
    file_server, session, tmp_path: Path
) -> None:
    """Reproduced before the fix: complete file, ETag changed, every GET 503.

    The old code truncated the data file inside ``resolve`` — before a single
    replacement byte had been requested — so a failed re-download left the user
    with nothing where a complete file used to be, and said nothing about it.
    Now the marker is dropped and an all-zero ledger is pinned in its place: the
    file is honestly described as 0 % complete and the bytes are still there.
    """
    first = task_for(file_server, "versioned", validator='"v1"')
    ok, _ = await run(file_server, session, tmp_path, first)
    assert ok
    target = tmp_path / "versioned"
    original = target.read_bytes()
    assert pc.load_completion(str(target)) is not None

    file_server.state.replacements[2] = original  # new revision, ETag "v2"
    file_server.state.version = 2
    file_server.state.fail_ranged = True  # ... that cannot be fetched at all

    second = task_for(file_server, "versioned", validator='"v2"')
    ok, _ = await run(file_server, session, tmp_path, second, retries=1)

    assert not ok
    assert target.read_bytes() == original, "the finished copy was destroyed"
    # The old marker described the old revision and had to go; the pinned ledger
    # is what stops the surviving bytes from being read as the new object.
    assert pc.load_completion(str(target)) is None
    pinned = pc.load(str(target))
    assert pinned is not None
    assert not any(pinned.bits)
    assert pinned.size == len(original)


# --------------------------------------------------------------------------- #
# If-Range is a request: the response's own validator is the evidence
# --------------------------------------------------------------------------- #
async def test_a_server_that_ignores_if_range_cannot_splice_two_revisions(
    file_server, session, tmp_path: Path
) -> None:
    """Reproduced before the fix: V1 prefix + V2 suffix, correct size, exit 0.

    ``/ignores-if-range`` honours ``Range`` and answers every piece with a
    ``206`` whose ``Content-Range`` is exact — drawn from whatever revision is
    current. Nothing in the status line or the window reveals that, so before
    the response validator was checked the two revisions were stitched together
    and a completion marker was written for the result.
    """
    original = file_server.data
    replacement = bytes(byte ^ 0xFF for byte in original)
    file_server.state.replacements[2] = replacement
    file_server.state.switch_after_ranges = 1  # swap once, mid-transfer

    task = task_for(file_server, "ignores-if-range", validator='"v1"')
    ok, _ = await run(file_server, session, tmp_path, task, connections=2, piece_size=PIECE)

    assert ok
    on_disk = (tmp_path / "ignores-if-range").read_bytes()
    assert len(on_disk) == len(original)
    assert sha256(on_disk) == sha256(replacement), "the file holds two revisions"
    assert sha256(on_disk) != sha256(original)
    # The first pieces were requested conditional on the revision we held.
    assert '"v1"' in file_server.state.if_ranges


# --------------------------------------------------------------------------- #
# a failure is classified before it is reported
# --------------------------------------------------------------------------- #
async def test_the_finish_record_carries_the_failure_reason(
    file_server, session, tmp_path: Path
) -> None:
    """Reproduced before the fix: ``finish`` had no ``reason`` for engine failures.

    The reporter reads ``task.meta['failure']`` while emitting the record, and
    the engine used to classify the failure *after* notifying it, so a machine
    could not tell a retryable 5xx whose budget ran out from a 404. Probe
    failures went through a different path and did carry the field, which is why
    the gap survived a release.
    """
    task = task_for(file_server, "flaky")
    ok, reporter = await run(file_server, session, tmp_path, task, retries=1)

    assert not ok
    assert task.meta.get("failure") == FAILURE_TRANSIENT_EXHAUSTED
    assert reporter.seen[-1] == (False, FAILURE_TRANSIENT_EXHAUSTED, None)


def test_the_json_reporter_emits_the_reason_it_was_given(tmp_path: Path) -> None:
    """The field is read at emit time, so ordering is the whole contract."""
    from downctl.builtin.reporters.json_reporter import JSONReporter

    task = DownloadTask(url=URL, target="model.bin", size=10)
    with (tmp_path / "out.jsonl").open("w", encoding="utf-8") as stream:
        reporter = JSONReporter(stream=stream)
        reporter.task_started(task)
        task.meta["failure"] = FAILURE_TRANSIENT_EXHAUSTED
        reporter.task_finished(task, False)

    record = json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "finish"
    assert record["ok"] is False
    assert record["reason"] == FAILURE_TRANSIENT_EXHAUSTED


# --------------------------------------------------------------------------- #
# a skip may not claim a comparison it did not make, nor erase one it inherited
# --------------------------------------------------------------------------- #
async def test_a_skip_without_a_probe_neither_claims_nor_erases_the_witness(
    session, tmp_path: Path
) -> None:
    """A local/SFTP transport observes no version, so it compares nothing.

    Reproduced before the fix: the marker's recorded witness was reported as
    ``validator`` proof even though nothing had been compared, and the same
    write blanked the recorded witness — so the next HTTP run, which *could*
    have compared it, found nothing left to compare.
    """
    payload = b"x" * 100
    target = tmp_path / "local.bin"
    target.write_bytes(payload)
    identity = url_identity(URL)
    assert pc.save_completion(str(target), identity, validator='"v1"')

    # No ``meta['validator']``: nothing observed the remote object.
    task = DownloadTask(url=URL, target="local.bin", size=len(payload))
    ok, reporter = await run(None, session, tmp_path, task, skip_existing=True)

    assert ok
    assert task.meta.get("witness") != "validator", "claimed a comparison it never made"
    assert reporter.seen[-1][2] != "validator"
    record = pc.load_completion(str(target))
    assert record is not None
    assert record["validator"] == '"v1"', "erased a witness it could not check"


async def test_a_skip_that_did_compare_still_claims_the_validator(
    file_server, session, tmp_path: Path
) -> None:
    """The mirror image: a real comparison is still reported as one."""
    first = task_for(file_server, "versioned", validator='"v1"')
    ok, _ = await run(file_server, session, tmp_path, first)
    assert ok

    second = task_for(file_server, "versioned", validator='"v1"')
    ok, reporter = await run(file_server, session, tmp_path, second, skip_existing=True)

    assert ok
    assert second.meta.get("witness") == "validator"
    record = pc.load_completion(str(tmp_path / "versioned"))
    assert record is not None and record["validator"] == '"v1"'


def test_rewriting_a_marker_keeps_the_digest_it_already_recorded(tmp_path: Path) -> None:
    """A run without ``--checksum`` must not blank the digest a run with one proved."""
    target = tmp_path / "digest.bin"
    target.write_bytes(b"y" * 32)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert pc.save_completion(str(target), url_identity(URL), "sha256", digest)

    assert _recorded_proof(pc.load_completion(str(target))) == ("", "sha256", digest)
    assert _recorded_proof(None) == ("", "", "")


# --------------------------------------------------------------------------- #
# verify: exact attribution, a scope the manifest defines, and no fail-open
# --------------------------------------------------------------------------- #
def verify_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "path": str(tmp_path),
        "json": True,
        "checksums": None,
        "repair": False,
        "local_plugins": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


async def test_an_empty_object_downloads_instead_of_looking_dead(
    file_server, tmp_path: Path
) -> None:
    """A zero-length object is an object, not a broken target.

    The probe only accepted a ``HEAD`` that reported a *positive* length, so it
    fell through to ``Range: bytes=0-0`` — unsatisfiable for an empty body — and
    reported ``probe failed: HTTP 416`` for a file that was there all along.
    """
    args = app._default_get_args()
    args.targets = [file_server.url("empty")]
    args.dir = str(tmp_path / "dl")
    args.reporter = "quiet"

    assert await app._get(args) == 0
    downloaded = tmp_path / "dl" / "empty"
    assert downloaded.exists()
    assert downloaded.read_bytes() == b""


async def test_the_same_target_twice_is_one_download(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Typing one URL twice must not plan two downloads of the same file.

    With ``-j 2`` both tasks started, and the loser reported "locked by another
    process" — a failure that says nothing about the object or the network. The
    plan is asserted rather than the race: whether the two overlapped depended on
    how fast the first one finished, so an outcome-based test would have been a
    coin flip that passed even with the duplicate still in place.
    """
    url = file_server.url("versioned")
    args = app._default_get_args()
    args.targets = [url, url]
    args.dir = str(tmp_path / "dl")
    args.reporter = "json"
    args.jobs = 2
    args.dry_run = True

    assert await app._get(args) == 0
    planned = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    assert len([item for item in planned if item.get("event") == "dry_run"]) == 1


def test_repair_never_deletes_a_file_the_manifest_did_not_name(tmp_path: Path) -> None:
    """Reproduced before the fix: a bare-name manifest entry deleted a nested file.

    The lookup fell back to ``basename``, so a manifest naming only ``data.bin``
    was applied to ``v2/data.bin`` — a different object the manifest never
    mentioned — which then read as a mismatch and was removed by ``--repair``.
    """
    nested = tmp_path / "v2" / "data.bin"
    nested.parent.mkdir()
    nested.write_bytes(b"the nested object")
    assert pc.save_completion(str(nested), url_identity(URL), validator='"v1"')
    root = tmp_path / "data.bin"
    root.write_bytes(b"the root object")
    assert pc.save_completion(str(root), url_identity(URL), validator='"v1"')

    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{sha256(b'the root object')}  data.bin\n", encoding="utf-8")

    code = app.cmd_verify(verify_args(tmp_path, checksums=str(sums), repair=True))

    assert nested.exists(), "a file the manifest never named was deleted"
    assert nested.read_bytes() == b"the nested object"
    assert root.exists()
    # The nested file is a claim the manifest says nothing about, so it is
    # reported as unprovable (exit 2) — never judged against another file's
    # digest, and never repaired.
    assert code == 2


def test_the_verify_command_checks_what_only_the_manifest_names(tmp_path: Path) -> None:
    """A manifest is a claim about what should be there, so it defines the scope.

    Reproduced before the fix: a two-entry manifest with one marker-less file
    exited 0 without ever reading that file, which reads exactly like "proven".
    """
    good = b"the real bytes"
    (tmp_path / "good.bin").write_bytes(good)
    assert pc.save_completion(str(tmp_path / "good.bin"), url_identity(URL))
    (tmp_path / "other.bin").write_bytes(b"corrupted!!!!!")

    sums = tmp_path / "SHA256SUMS"
    sums.write_text(
        f"{sha256(good)}  good.bin\n{sha256(b'expected bytes')}  other.bin\n",
        encoding="utf-8",
    )

    assert app.cmd_verify(verify_args(tmp_path, checksums=str(sums))) == 1


def test_verify_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    """Failing open reported "nothing to verify" and exited 0 for a typo."""
    not_a_dir = tmp_path / "file.bin"
    not_a_dir.write_bytes(b"x")

    assert app.cmd_verify(verify_args(not_a_dir, json=False)) == 1
    assert app.cmd_verify(verify_args(tmp_path / "missing", json=False)) == 1


def test_config_show_lists_every_core_option(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Reproduced before the fix: three options were invisible in ``config show``.

    The output was a hand-written list, so ``max_time``, ``probe_jobs`` and
    ``require_witness`` shipped without ever appearing in the output a user
    checks their file against. The list is now derived from ``CoreConfig``, so
    this test fails for any option added later that nobody formats.
    """
    from dataclasses import fields as dataclass_fields

    from downctl.config import CoreConfig

    monkeypatch_config = tmp_path / "config.toml"
    assert app.cmd_config(argparse.Namespace(config_action="show")) == 0
    shown = capsys.readouterr().out

    for info in dataclass_fields(CoreConfig):
        assert f"{info.name}" in shown, f"config show hides core.{info.name}"
    assert str(monkeypatch_config) not in shown  # sanity: no stray path
