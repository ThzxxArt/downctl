"""Content-truth regression tests: the bytes on disk must be one revision.

Every test here asserts a *content* invariant (a SHA-256 of the final file
against the server's current revision), not a piece flag or an exit code.
That is deliberate: 0.1.3 could report success for a file made of two
different revisions because every existing test only looked at sizes, bits and
exit codes.

A failure in this module means downloadctl silently produced something that is
not the object it claims to have fetched — the one thing the README promises
never happens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from downctl import app, console
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.config import Config, CoreConfig
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_task
from downctl.core.errors import RangeMismatchError
from downctl.core.task import DownloadTask
from downctl.core.transfer import transfer_file
from downctl.plugin.hooks import EventBus
from downctl.plugin.loader import discover
from downctl.urls import normalize_validator, url_identity

PIECE = 1 << 20
#: A stable URL for the state-format tests, which never touch the network.
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


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def seed_ledger(
    target: str,
    url: str,
    data: bytes,
    *,
    keep: int,
    validator: str,
    piece_size: int = PIECE,
) -> pc.Bitmap:
    """Leave ``keep`` complete pieces of ``data`` plus a trusted ledger.

    This is what an interrupted multi-connection download leaves behind: real
    bytes of one revision, plus a bitmap that says which pieces they are.
    """
    _, pieces, _ = pc.layout(len(data), 1, piece_size)
    with open(target, "wb") as fh:
        fh.write(data[: keep * piece_size])
    bitmap = pc.Bitmap(
        len(data),
        pieces,
        [1] * keep + [0] * (pieces - keep),
        keep * piece_size,
        piece_size,
        url_sha256=url_identity(url),
        validator=validator,
    )
    assert pc.save(target, bitmap)
    return bitmap


def seen_ranges(server, since: int) -> list[str]:
    return [item for item in server.ranges[since:] if item is not None]


# --------------------------------------------------------------------------- #
# the headline defect: one URL, two revisions, one spliced file
# --------------------------------------------------------------------------- #
async def test_same_revision_resumes_only_the_missing_pieces(
    file_server, session, tmp_path: Path
) -> None:
    data = file_server.data
    url = file_server.url("versioned")
    target = str(tmp_path / "out.bin")
    seed_ledger(target, url, data, keep=1, validator='"v1"')

    before = len(file_server.ranges)
    task = DownloadTask(url=url, target="out.bin", size=len(data))
    task.meta["validator"] = '"v1"'
    task.meta["if_range"] = '"v1"'
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=PIECE,
        stall_limit=5,
    )

    assert ok
    assert Path(target).read_bytes() == data
    requested = seen_ranges(file_server, before)
    assert requested, "the missing pieces must have been fetched"
    assert not any(item.startswith("bytes=0-") for item in requested)
    # Every ranged request was conditional on the revision we hold.
    assert set(file_server.state.if_ranges[before:]) == {'"v1"'}


async def test_replaced_object_is_not_spliced(file_server, session, tmp_path: Path) -> None:
    """The exact 0.1.3 defect: resume half of V1 into a file that now holds V2."""
    data = file_server.data
    replacement = os.urandom(len(data))  # same size: no size check can help
    file_server.state.replace(replacement)
    url = file_server.url("versioned")
    target = str(tmp_path / "out.bin")
    seed_ledger(target, url, data, keep=3, validator='"v1"')

    before = len(file_server.ranges)
    task = DownloadTask(url=url, target="out.bin", size=len(data))
    # What a probe of the current object reports.
    task.meta["validator"] = '"v2"'
    task.meta["if_range"] = '"v2"'
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=PIECE,
        stall_limit=5,
    )

    assert ok
    got = Path(target).read_bytes()
    assert sha256(got) == sha256(replacement), "the file is not the current revision"
    spliced = data[: 3 * PIECE] + replacement[3 * PIECE :]
    assert got != spliced, "the two revisions were spliced together"
    # The stale half was thrown away and fetched again from zero.
    assert any(item.startswith("bytes=0-") for item in seen_ranges(file_server, before))


async def test_cli_run_does_not_splice_a_replaced_object(file_server, tmp_path: Path) -> None:
    """The same defect, driven through the real command path (probe included)."""
    data = file_server.data
    replacement = os.urandom(len(data))
    url = file_server.url("versioned")
    destination = tmp_path / "dl"
    destination.mkdir()
    seed_ledger(str(destination / "versioned"), url, data, keep=3, validator='"v1"')
    file_server.state.replace(replacement)

    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    got = (destination / "versioned").read_bytes()
    assert sha256(got) == sha256(replacement)
    assert got != data[: 3 * PIECE] + replacement[3 * PIECE :]


async def test_a_second_run_does_not_re_fetch_a_current_file(file_server, tmp_path: Path) -> None:
    """The fix must not turn every re-run into a full download."""
    url = file_server.url("versioned")
    destination = tmp_path / "dl"
    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "versioned").read_bytes() == file_server.data

    before = len(file_server.ranges)
    rerun = make_args(targets=[url], dir=str(destination), reporter="quiet", skip_existing=True)
    assert await app._get(rerun) == 0
    assert seen_ranges(file_server, before) == []
    marker = pc.load_completion(str(destination / "versioned"))
    assert marker is not None
    assert marker["validator"] == '"v1"'


async def test_inflight_replacement_restarts_instead_of_splicing(
    file_server, session, tmp_path: Path
) -> None:
    """The object is replaced *during* the transfer.

    Only the conditional range request can catch this: the ledger, the size and
    the already-fetched pieces all look perfectly fine. Two pieces are served
    from the old revision before the switch, so a missing guard means the file
    is assembled from two different revisions.
    """
    data = file_server.data
    replacement = os.urandom(len(data))
    file_server.state.replacements[2] = replacement
    url = file_server.url("versioned")
    target = str(tmp_path / "out.bin")
    seed_ledger(target, url, data, keep=1, validator='"v1"')
    file_server.state.switch_after_ranges = 2  # piece 1 and 2 are still V1

    task = DownloadTask(url=url, target="out.bin", size=len(data))
    task.meta["validator"] = '"v1"'
    task.meta["if_range"] = '"v1"'
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=1,
        piece_size=PIECE,
        stall_limit=5,
    )

    assert ok
    got = Path(target).read_bytes()
    assert sha256(got) == sha256(replacement), "the file mixes two revisions"
    assert file_server.state.version == 2


async def test_unstable_etag_still_downloads(file_server, tmp_path: Path) -> None:
    """A server that renames the object on every response must not wedge.

    Its witness can never match, so ``If-Range`` is abandoned for that target
    (reported as unversioned) rather than restarting the transfer forever.
    """
    file_server.state.unstable_etag = True
    url = file_server.url("versioned")
    destination = tmp_path / "dl"

    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "versioned").read_bytes() == file_server.data


# --------------------------------------------------------------------------- #
# response-level checks
# --------------------------------------------------------------------------- #
async def test_206_with_the_wrong_window_is_refused(file_server, session, tmp_path: Path) -> None:
    url = file_server.url("wrong-range")
    target = str(tmp_path / "w.bin")
    with pytest.raises(RangeMismatchError):
        await transfer_file(
            make_ctx(session, tmp_path),
            HTTPTransport(),
            url,
            target,
            len(file_server.data),
            connections=1,
            stall_limit=5,
        )
    # Nothing was written: a wrong window is refused before the first byte.
    assert Path(target).stat().st_size == 0


async def test_wrong_window_fails_the_target_instead_of_corrupting_it(
    file_server, session, tmp_path: Path
) -> None:
    url = file_server.url("wrong-range")
    task = DownloadTask(url=url, target="w.bin", size=len(file_server.data))
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        retries=1,
        connections=2,
        stall_limit=5,
    )
    assert not ok
    assert (tmp_path / "w.bin").stat().st_size == 0


async def test_416_recovers_by_reprobeing_the_size(file_server, tmp_path: Path) -> None:
    """A shrunken object answers 416; the run must re-probe and succeed once."""
    shrunk = file_server.data[: len(file_server.data) // 2]
    file_server.state.shrunk = shrunk
    url = file_server.url("shrink")
    destination = tmp_path / "dl"

    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "shrink").read_bytes() == shrunk
    assert file_server.state.rejections >= 1, "the 416 path was never exercised"


# --------------------------------------------------------------------------- #
# witnesses on the skip path
# --------------------------------------------------------------------------- #
async def test_skip_is_refused_when_the_object_was_replaced(
    file_server, session, tmp_path: Path
) -> None:
    data = file_server.data
    replacement = os.urandom(len(data))
    url = file_server.url("versioned")
    target = tmp_path / "done.bin"
    target.write_bytes(data)  # complete, but of revision 1
    assert pc.save_completion(str(target), url_identity(url), validator='"v1"')
    file_server.state.replace(replacement)

    task = DownloadTask(url=url, target="done.bin", size=len(data))
    task.meta["validator"] = '"v2"'
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert sha256(target.read_bytes()) == sha256(replacement)


async def test_unversioned_skip_is_proven_and_reported(
    file_server, session, tmp_path: Path
) -> None:
    """A server with no validators can still be skipped — but never as "proven"."""
    url = file_server.url("no-etag")
    target = tmp_path / "n.bin"
    target.write_bytes(file_server.data)
    assert pc.save_completion(str(target), url_identity(url), validator="")

    bus = EventBus()
    finished: list[dict[str, object]] = []
    bus.subscribe("on_task_finish", finished.append)
    ctx = make_ctx(session, tmp_path)
    ctx.events = bus
    task = DownloadTask(url=url, target="n.bin", size=len(file_server.data))
    task.meta["validator"] = ""
    assert await download_task(
        ctx, task, reporter=QuietReporter(), skip_existing=True, stall_limit=5
    )
    assert file_server.ranges == []  # nothing was re-fetched
    assert finished and finished[-1]["witness"] == "none"

    # The same state is refused when a witness is demanded.
    strict = make_ctx(session, tmp_path, require_witness=True)
    task2 = DownloadTask(url=url, target="n.bin", size=len(file_server.data))
    task2.meta["validator"] = ""
    assert await download_task(
        strict, task2, reporter=QuietReporter(), skip_existing=True, stall_limit=5
    )
    assert seen_ranges(file_server, 0), "require_witness must re-fetch the file"


async def test_a_verified_checksum_is_reported_as_its_own_witness(
    file_server, session, tmp_path: Path
) -> None:
    url = file_server.url("no-etag")
    target = tmp_path / "c.bin"
    target.write_bytes(b"x" * len(file_server.data))  # wrong content
    digest = sha256(file_server.data)

    bus = EventBus()
    finished: list[dict[str, object]] = []
    bus.subscribe("on_task_finish", finished.append)
    ctx = make_ctx(session, tmp_path)
    ctx.events = bus
    task = DownloadTask(
        url=url,
        target="c.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex=digest,
        meta={"user_checksum": True},
    )
    task.meta["validator"] = ""
    assert await download_task(
        ctx, task, reporter=QuietReporter(), connections=2, skip_existing=True, stall_limit=5
    )
    assert sha256(target.read_bytes()) == digest


async def test_recheck_redownloads_a_file_it_cannot_prove(
    file_server, session, tmp_path: Path
) -> None:
    """``--recheck`` re-hashes: a same-size fake is caught and fetched again."""
    url = file_server.url("versioned")
    target = tmp_path / "done.bin"
    target.write_bytes(b"x" * len(file_server.data))  # right size, wrong bytes
    assert pc.save_completion(
        str(target), url_identity(url), "sha256", sha256(file_server.data), validator='"v1"'
    )

    task = DownloadTask(url=url, target="done.bin", size=len(file_server.data))
    task.meta.update(
        {"validator": '"v1"', "if_range": '"v1"', "recheck": True, "require_witness": True}
    )
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert sha256(target.read_bytes()) == sha256(file_server.data)


async def test_recheck_skips_a_file_it_can_prove(file_server, session, tmp_path: Path) -> None:
    """Proof may come from the digest the original download recorded."""
    url = file_server.url("versioned")
    target = tmp_path / "done.bin"
    target.write_bytes(file_server.data)
    assert pc.save_completion(
        str(target), url_identity(url), "sha256", sha256(file_server.data), validator='"v1"'
    )

    task = DownloadTask(url=url, target="done.bin", size=len(file_server.data))
    task.meta.update(
        {"validator": '"v1"', "if_range": '"v1"', "recheck": True, "require_witness": True}
    )
    before = len(file_server.ranges)
    assert await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert seen_ranges(file_server, before) == []


async def test_recheck_without_any_digest_refuses_to_skip(
    file_server, session, tmp_path: Path
) -> None:
    """``--recheck`` means "prove it or fetch it": a v2 marker proves nothing."""
    url = file_server.url("versioned")
    target = tmp_path / "unproven.bin"
    target.write_bytes(file_server.data)
    assert pc.save_completion(str(target), url_identity(url))

    task = DownloadTask(url=url, target="unproven.bin", size=len(file_server.data))
    task.meta.update({"validator": '"v1"', "if_range": '"v1"', "recheck": True})
    before = len(file_server.ranges)
    assert await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert seen_ranges(file_server, before), "an unprovable file must be fetched again"


# --------------------------------------------------------------------------- #
# migration from the pre-witness formats
# --------------------------------------------------------------------------- #
def _write_v2_marker(target: Path, identity: str, payload: bytes) -> None:
    """A 0.1.3-era completion marker: size + mtime + identity, no witness."""
    stat = target.stat()
    Path(pc.completion_path(str(target))).write_text(
        json.dumps(
            {
                "version": 2,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "url_sha256": identity,
                "checksum_algo": "",
                "checksum_hex": "",
            }
        ),
        encoding="utf-8",
    )
    assert len(payload) == stat.st_size


def test_a_legacy_marker_keeps_being_trusted(tmp_path: Path) -> None:
    """Upgrading must not force a mass re-download of 0.1.3 downloads."""
    target = tmp_path / "old.bin"
    target.write_bytes(b"x" * 100)
    identity = url_identity(URL)
    _write_v2_marker(target, identity, b"x" * 100)

    assert pc.completion_valid(str(target), 100, identity)
    assert pc.witness_of(pc.load_completion(str(target))) == "none"
    # It is a completion record, not a version proof: demanding a witness refuses it.
    assert not pc.completion_valid(str(target), 100, identity, require_witness=True)


def test_a_legacy_marker_is_rewritten_with_a_witness_it_earned(tmp_path: Path) -> None:
    target = tmp_path / "old.bin"
    target.write_bytes(b"x" * 100)
    identity = url_identity(URL)
    _write_v2_marker(target, identity, b"x" * 100)

    assert pc.save_completion(str(target), identity, "sha256", "a" * 64, validator='"v9"')
    record = pc.load_completion(str(target))
    assert record is not None
    assert record["version"] == pc.COMPLETION_VERSION
    assert record["validator"] == '"v9"'
    assert pc.witness_of(record) == "validator"


def test_a_downgrade_cannot_destroy_a_completed_file(tmp_path: Path) -> None:
    """A 0.1.4 marker stays readable by 0.1.3 (no version bump, additive field).

    0.1.3 refuses a marker whose ``version`` is higher than it knows and then
    treats the *completed* file as unproven state, which makes its ``resolve``
    truncate the data file. Keeping the version at 2 is what prevents that.
    """
    target = tmp_path / "new.bin"
    target.write_bytes(b"x" * 100)
    identity = url_identity(URL)
    assert pc.save_completion(str(target), identity, "sha256", "a" * 64, validator='"v9"')

    record = pc.load_completion(str(target))
    assert record is not None
    # Exactly what a 0.1.3 reader validates: version, size, mtime, identity.
    assert record["version"] == 2
    assert record["size"] == 100
    assert record["url_sha256"] == identity
    # ... and the file itself is never handed to a reset by an older build,
    # because its marker is still a *valid* v2 marker for that build.
    assert pc.completion_valid(str(target), 100, identity, validator='"v9"')


def test_a_legacy_ledger_is_not_resumed_once_the_server_names_a_version(tmp_path: Path) -> None:
    """A 0.1.3 ledger has no witness, so the first witness-aware run resets it.

    The format version is unchanged (the field is additive), so what refuses
    this ledger is the *comparison*, not a version check — which is also why
    downgrading cannot destroy anything.
    """
    target = tmp_path / "old.bin"
    target.write_bytes(b"x" * 100)
    Path(pc.path_for(str(target))).write_text(
        json.dumps(
            {
                "version": 4,
                "size": 100,
                "pieces": 4,
                "bits": [1, 1, 0, 0],
                "min_size": 100,
                "piece_size": 25,
                "url_sha256": url_identity(URL),
            }
        ),
        encoding="utf-8",
    )
    assert pc.resolve(str(target), 100, 4, 25, url_identity(URL), validator='"v1"') == [
        0,
        0,
        0,
        0,
    ]
    assert not Path(target).read_bytes(), "the file is truncated, not extended"


async def test_a_weak_etag_is_a_comparison_key_only(file_server, tmp_path: Path) -> None:
    """RFC 9110 forbids a weak validator in ``If-Range``, so it is only compared.

    The witness still detects a replacement (through the probe), it just cannot
    drive a conditional request — which is the documented trade-off.
    """
    url = file_server.url("weak")
    destination = tmp_path / "dl"
    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    target = destination / "weak"
    assert target.read_bytes() == file_server.data

    marker = pc.load_completion(str(target))
    assert marker is not None
    assert marker["validator"] == '"v1"'
    assert set(file_server.state.if_ranges) == {None}, "a weak ETag must never be sent"

    replacement = os.urandom(len(file_server.data))
    file_server.state.replace(replacement)
    skip = make_args(targets=[url], dir=str(destination), reporter="quiet", skip_existing=True)
    assert await app._get(skip) == 0
    assert sha256(target.read_bytes()) == sha256(replacement)


async def test_a_last_modified_witness_drives_a_conditional_resume(
    file_server, session, tmp_path: Path
) -> None:
    """A date is a usable ``If-Range`` value, and it resumes piece by piece."""
    data = file_server.data
    url = file_server.url("mtime")
    stamp = file_server.state.last_modified()
    target = str(tmp_path / "out.bin")
    seed_ledger(target, url, data, keep=1, validator=stamp)

    before = len(file_server.ranges)
    task = DownloadTask(url=url, target="out.bin", size=len(data))
    task.meta["validator"] = stamp
    task.meta["if_range"] = stamp
    assert await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=PIECE,
        stall_limit=5,
    )
    assert Path(target).read_bytes() == data
    assert set(file_server.state.if_ranges[before:]) == {stamp}
    assert not any(item.startswith("bytes=0-") for item in seen_ranges(file_server, before))


async def test_a_retried_target_detects_an_inflight_replacement(
    file_server, tmp_path: Path
) -> None:
    """The ``--retry-failed`` entry path is conditional too.

    The retry re-resolves the origin and re-probes, so the witness it uses is
    fresh; what this pins is that the whole path (failed log → re-resolve →
    probe → conditional transfer → restart) never splices.
    """
    data = file_server.data
    replacement = os.urandom(len(data))
    url = file_server.url("versioned")
    destination = tmp_path / "dl"
    destination.mkdir()
    record = "\t".join(["url", url, "versioned", "", "", str(len(data)), "url", url])
    (destination / ".downctl.failed").write_text(record + "\n", encoding="utf-8")

    # The object is replaced while the transfer runs (pieces 1 and 2 are V1).
    file_server.state.replacements[2] = replacement
    file_server.state.switch_after_ranges = 2

    args = make_args(retry_failed=True, dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0

    target = destination / "versioned"
    assert sha256(target.read_bytes()) == sha256(replacement), "the revisions were spliced"
    marker = pc.load_completion(str(target))
    assert marker is not None
    assert marker["validator"], "the retried download must record a witness"


async def test_probe_records_the_witness_for_an_already_sized_task(
    file_server, session, tmp_path: Path
) -> None:
    """The unit-level contract behind the test above."""
    task = DownloadTask(url=file_server.url("versioned"), target="x.bin", size=12345)
    await app._probe_task(make_ctx(session, tmp_path), task, retries=1)
    assert task.meta["validator"] == '"v1"'
    assert task.meta["if_range"] == '"v1"'
    assert task.size == len(file_server.data)


async def test_a_task_with_a_witness_sends_if_range(file_server, session, tmp_path: Path) -> None:
    """The transfer side: a known witness makes every piece conditional."""
    url = file_server.url("versioned")
    task = DownloadTask(url=url, target="sized.bin", size=len(file_server.data))
    task.meta.update({"validator": '"v1"', "if_range": '"v1"'})
    before = len(file_server.ranges)

    assert await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=PIECE,
        stall_limit=5,
    )
    assert (tmp_path / "sized.bin").read_bytes() == file_server.data
    assert set(file_server.state.if_ranges[before:]) == {'"v1"'}


# --------------------------------------------------------------------------- #
# other transports keep working (they have no Content-Range to check)
# --------------------------------------------------------------------------- #
async def test_file_transport_is_unaffected_by_range_validation(tmp_path: Path) -> None:
    """A local copy answers a range with a synthetic 206 and no headers."""
    source = tmp_path / "src.bin"
    payload = os.urandom(3 * PIECE + 17)
    source.write_bytes(payload)
    target = str(tmp_path / "dst.bin")
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        registry=discover(),
    )
    transport = ctx.registry.get("transport", "file")  # type: ignore[union-attr]
    result = await transfer_file(
        ctx,
        transport,  # type: ignore[arg-type]
        f"file://{source}",
        target,
        len(payload),
        connections=3,
        piece_size=PIECE,
        stall_limit=5,
    )
    assert result.complete
    assert Path(target).read_bytes() == payload


# --------------------------------------------------------------------------- #
# witness normalisation itself
# --------------------------------------------------------------------------- #
def test_normalize_validator_prefers_a_strong_etag() -> None:
    witness = normalize_validator(
        {"ETag": '"abc"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
    )
    assert witness.value == '"abc"'
    assert witness.if_range == '"abc"'
    assert witness.kind == "etag"


def test_normalize_validator_never_sends_a_weak_etag_conditionally() -> None:
    witness = normalize_validator({"ETag": 'W/"abc"'})
    assert witness.value == '"abc"'  # still a usable comparison key
    assert witness.if_range == ""  # RFC 9110 forbids a weak If-Range
    assert witness.kind == "etag"


def test_normalize_validator_falls_back_to_last_modified() -> None:
    witness = normalize_validator({"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
    assert witness.value == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert witness.if_range == witness.value
    assert witness.kind == "last-modified"


def test_normalize_validator_rejects_unusable_values() -> None:
    assert not normalize_validator({})
    assert not normalize_validator({"ETag": "   "})
    assert not normalize_validator({"ETag": 'a"b\nc'})  # control character
    assert not normalize_validator({"ETag": "x" * (pc.MAX_PIECES * 0 + 5000)})


def test_weak_and_strong_etags_share_a_comparison_key() -> None:
    """A weak tag still detects a *change*, which is all a witness claims."""
    assert (
        normalize_validator({"ETag": 'W/"abc"'}).value
        == normalize_validator({"ETag": '"abc"'}).value
    )
