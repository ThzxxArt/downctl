"""Tests for the metrics and notify hook plugins."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl.builtin.hooks.metrics_hook import MetricsHook
from downctl.builtin.hooks.notify_hook import NotifyHook
from downctl.core.task import DownloadTask
from downctl.plugin.hooks import EventBus, apply_hook_options

_FINISH: dict[str, object] = {
    "succeeded": 3,
    "failed": 2,
    "hooks_failed": 1,
    "bytes": 12345,
    "elapsed": 1.5,
}


# --- metrics hook --------------------------------------------------------- #
def test_metrics_writes_every_metric(tmp_path: Path) -> None:
    target = tmp_path / "run.prom"
    MetricsHook(path=str(target)).on_event("on_finish", _FINISH)
    assert target.read_text(encoding="utf-8").splitlines() == [
        "downctl_run_duration_seconds 1.5",
        'downctl_files_total{outcome="ok"} 3',
        'downctl_files_total{outcome="failed"} 2',
        "downctl_bytes_total 12345",
        'downctl_failures_total{reason="unknown"} 2',
        "downctl_hook_failures_total 1",
    ]


def test_metrics_missing_payload_keys_default_to_zero(tmp_path: Path) -> None:
    target = tmp_path / "run.prom"
    MetricsHook(path=str(target)).on_event("on_finish", {})
    assert target.read_text(encoding="utf-8").splitlines() == [
        "downctl_run_duration_seconds 0",
        'downctl_files_total{outcome="ok"} 0',
        'downctl_files_total{outcome="failed"} 0',
        "downctl_bytes_total 0",
        'downctl_failures_total{reason="unknown"} 0',
        "downctl_hook_failures_total 0",
    ]


def test_metrics_labels_are_rendered_and_escaped(tmp_path: Path) -> None:
    target = tmp_path / "run.prom"
    hook = MetricsHook(path=str(target), labels=["env=prod", 'msg=a"b\\c'])
    hook.on_event("on_finish", _FINISH)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == 'downctl_run_duration_seconds{env="prod",msg="a\\"b\\\\c"} 1.5'
    assert lines[1] == 'downctl_files_total{outcome="ok",env="prod",msg="a\\"b\\\\c"} 3'
    assert lines[4] == 'downctl_failures_total{reason="unknown",env="prod",msg="a\\"b\\\\c"} 2'


def test_metrics_reports_each_failure_reason(tmp_path: Path) -> None:
    """The engine's breakdown becomes one series per reason, not one guess."""
    target = tmp_path / "run.prom"
    payload = dict(_FINISH)
    payload["failures_by_reason"] = {"permanent": 1, "transient-exhausted": 1}
    MetricsHook(path=str(target)).on_event("on_finish", payload)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert 'downctl_failures_total{reason="permanent"} 1' in lines
    assert 'downctl_failures_total{reason="transient-exhausted"} 1' in lines
    assert not any('reason="unknown"' in line for line in lines)


def test_metrics_atomic_replace_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "run.prom"
    target.write_text("OLD\n", encoding="utf-8")
    hook = MetricsHook(path=str(target))
    hook.on_event("on_finish", _FINISH)
    assert "OLD" not in target.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


async def test_metrics_write_failure_warns_and_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "run.prom"
    target.mkdir()  # a directory cannot be replaced by a file
    hook = MetricsHook(path=str(target))
    hook.on_event("on_finish", _FINISH)
    assert "metrics: could not write" in capsys.readouterr().err
    assert await hook.aclose() == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_metrics_ignores_other_events_and_uses_the_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    hook = MetricsHook()
    assert hook.path == "downctl.prom"
    assert hook.labels == []
    hook.on_event("on_start", {"targets": []})
    hook.on_event("on_error", {"error": "boom"})
    assert not (tmp_path / "downctl.prom").exists()


def test_metrics_options_are_applied(tmp_path: Path) -> None:
    target = tmp_path / "wired.prom"
    hook = MetricsHook()
    problems = apply_hook_options(hook, {"path": str(target), "labels": ["env=test"]})
    assert problems == []
    hook.on_event("on_finish", {})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert all('env="test"' in line for line in lines)


async def test_metrics_write_failure_reaches_the_event_bus(tmp_path: Path) -> None:
    """``strict_hooks`` reads ``EventBus.failures``, not just the hook's counter."""
    target = tmp_path / "run.prom"
    target.mkdir()
    bus = EventBus()
    bus.register_hook(MetricsHook(path=str(target)))
    bus.emit("on_finish", _FINISH)
    await bus.drain()
    assert bus.failures == 1


def test_metrics_rejects_malformed_labels() -> None:
    hook = MetricsHook()
    problems = hook.configure({"labels": ["missing-equals"]})
    assert len(problems) == 1
    assert "name=value" in problems[0]
    problems = hook.configure({"labels": [1]})
    assert len(problems) == 1
    assert "list of strings" in problems[0]
    # An illegal Prometheus name invalidates the whole textfile, and a repeated
    # name makes the series ambiguous; both are refused at configuration time.
    problems = hook.configure({"labels": ["1bad=x"]})
    assert len(problems) == 1
    assert "valid Prometheus label" in problems[0]
    problems = hook.configure({"labels": ["env=a", "env=b"]})
    assert len(problems) == 1
    assert "twice" in problems[0]


def test_metrics_normalizes_a_single_label_string() -> None:
    """The shared ``list`` option kind accepts one string, like ``events`` does."""
    hook = MetricsHook()
    assert hook.configure({"labels": "env=test"}) == []
    assert hook.labels == ["env=test"]


# --- notify hook ---------------------------------------------------------- #
@pytest_asyncio.fixture
async def sink():
    received: list[dict[str, object]] = []

    async def handler(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.Response(text="ok")

    application = web.Application()
    application.router.add_post("/notify", handler)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}/notify", received
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def failing_sink():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=503, text="unavailable")

    application = web.Application()
    application.router.add_post("/notify", handler)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}/notify"
    finally:
        await runner.cleanup()


async def test_notify_url_posts_the_expected_json(sink) -> None:
    url, received = sink
    hook = NotifyHook(url=url)
    hook.on_event(
        "on_finish",
        {"succeeded": 1, "failed": 2, "bytes": 100, "elapsed": 1.5},
    )
    assert await hook.aclose() == 0
    assert received == [
        {
            "title": "downctl",
            "message": "1 succeeded; 2 failed; 100 bytes; 1.5s elapsed",
            "url": url,
        }
    ]


async def test_notify_url_uses_the_title_and_message_for_a_generic_webhook(sink) -> None:
    url, received = sink
    hook = NotifyHook(url=url, title="deploy")
    hook.on_event("on_finish", {"succeeded": 2, "failed": 0, "bytes": 10})
    assert await hook.aclose() == 0
    assert received[0]["title"] == "deploy"
    assert received[0]["message"] == "2 succeeded; 0 failed; 10 bytes; 0s elapsed"
    assert set(received[0]) == {"title", "message", "url"}


async def test_notify_topic_uses_the_ntfy_payload_shape(sink) -> None:
    url, received = sink
    hook = NotifyHook(url=url, topic="my-downctl", title="nightly")
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0, "bytes": 7})
    assert await hook.aclose() == 0
    assert received == [
        {
            "topic": "my-downctl",
            "message": "1 succeeded; 0 failed; 7 bytes; 0s elapsed",
            "title": "nightly",
        }
    ]


async def test_notify_only_failures_skips_a_clean_finish(sink) -> None:
    url, received = sink
    hook = NotifyHook(url=url, only_failures=True)
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert await hook.aclose() == 0
    assert received == []

    hook.on_event("on_finish", {"succeeded": 0, "failed": 1})
    assert await hook.aclose() == 0
    assert len(received) == 1
    assert "1 failed" in str(received[0]["message"])


async def test_notify_ignores_unselected_events(sink) -> None:
    url, received = sink
    hook = NotifyHook(url=url, events=["on_start"])
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert await hook.aclose() == 0
    assert received == []


async def test_notify_body_lists_targets_and_redacts_embedded_urls(sink) -> None:
    url, received = sink
    task = DownloadTask(url="https://h/b.bin", target="b.bin")
    hook = NotifyHook(url=url, events=["on_start", "on_error"])
    hook.on_event(
        "on_start",
        {"targets": ["https://h/a.bin?token=SECRET", task]},
    )
    hook.on_event("on_error", {"error": "GET https://h/c.bin?token=SECRET failed"})
    assert await hook.aclose() == 0
    assert len(received) == 2

    # Deliveries are concurrent background tasks, so arrival order is not fixed.
    bodies = [str(item["message"]) for item in received]
    start_body = next(body for body in bodies if "targets:" in body)
    assert "targets: https://h/a.bin?token=***, b.bin" in start_body
    assert "SECRET" not in start_body

    error_body = next(body for body in bodies if "error:" in body)
    assert "error: GET https://h/c.bin?token=*** failed" in error_body
    assert "SECRET" not in error_body


async def test_notify_missing_backend_warns_and_records_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = NotifyHook()
    hook.on_event("on_finish", {"failed": 1})
    hook.on_event("on_finish", {"failed": 1})
    err = capsys.readouterr().err
    assert "set exactly one of url or command" in err
    assert err.count("set exactly one of url or command") == 1
    assert await hook.aclose() == 1


async def test_notify_command_gets_the_environment_and_aclose_waits(tmp_path: Path) -> None:
    script = tmp_path / "dump.py"
    script.write_text(
        "import os, sys, time\n"
        "time.sleep(0.2)\n"
        "keys = ('DOWNCTL_EVENT', 'DOWNCTL_SUCCEEDED', 'DOWNCTL_FAILED', "
        "'DOWNCTL_BYTES', 'DOWNCTL_ELAPSED')\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
        "    for key in keys:\n"
        "        fh.write(key + '=' + os.environ[key] + '\\n')\n",
        encoding="utf-8",
    )
    marker = tmp_path / "env.txt"
    hook = NotifyHook(command=f'"{sys.executable}" "{script}" "{marker}"')
    hook.on_event(
        "on_finish",
        {"succeeded": 2, "failed": 1, "bytes": 1024, "elapsed": 3.5},
    )
    assert await hook.aclose() == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "DOWNCTL_EVENT=on_finish",
        "DOWNCTL_SUCCEEDED=2",
        "DOWNCTL_FAILED=1",
        "DOWNCTL_BYTES=1024",
        "DOWNCTL_ELAPSED=3.5",
    ]


async def test_notify_nonzero_command_warns_and_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    hook = NotifyHook(command=f'"{sys.executable}" "{script}"')
    hook.on_event("on_finish", {"succeeded": 0, "failed": 1})
    assert await hook.aclose() == 1
    assert "notify: command exited with 3" in capsys.readouterr().err


async def test_notify_command_timeout_kills_the_process_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "too-late.txt"
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "time.sleep(30)\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
        "    fh.write('late')\n",
        encoding="utf-8",
    )
    hook = NotifyHook(command=f'"{sys.executable}" "{script}" "{marker}"', timeout=0.2)
    started = asyncio.get_running_loop().time()
    hook.on_event("on_finish", {"succeeded": 0, "failed": 1})
    assert await hook.aclose() == 1
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.0
    assert not marker.exists()
    err = capsys.readouterr().err
    assert "timed out after 0.2s" in err
    assert "terminated" in err


def test_notify_timeout_option_is_positive_and_defaults_to_ten_seconds() -> None:
    hook = NotifyHook()
    assert hook.timeout == 10.0
    assert hook.configure({"timeout": 2}) == []
    assert hook.timeout == 2.0

    problems = NotifyHook().configure({"timeout": 0})
    assert len(problems) == 1
    assert "positive" in problems[0]


async def test_notify_empty_exception_message_still_names_the_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class EmptyFailureSession:
        """A session whose POST fails with an exception that has no text."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> EmptyFailureSession:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> object:
            raise TimeoutError()

    monkeypatch.setattr(aiohttp, "ClientSession", EmptyFailureSession)
    hook = NotifyHook(url="http://127.0.0.1:1/notify")
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert await hook.aclose() == 1
    err = capsys.readouterr().err
    assert "failed: TimeoutError" in err


async def test_notify_command_keeps_json_stdout_clean(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    from downctl import console

    script = tmp_path / "hello.py"
    script.write_text("print('NOTIFY-OUTPUT')\n", encoding="utf-8")
    hook = NotifyHook(command=f'"{sys.executable}" "{script}"')
    console.set_machine_readable(True)
    try:
        hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
        assert await hook.aclose() == 0
    finally:
        console.set_machine_readable(False)
    out, err = capfd.readouterr()
    assert "NOTIFY-OUTPUT" not in out
    assert "NOTIFY-OUTPUT" in err


async def test_notify_http_error_warns_and_records(
    failing_sink, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = NotifyHook(url=failing_sink)
    hook.on_event("on_finish", {"succeeded": 0, "failed": 1})
    assert await hook.aclose() == 1
    assert "HTTP 503" in capsys.readouterr().err


async def test_notify_masks_credentials_in_a_failed_post(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = NotifyHook(url="http://127.0.0.1:1/notify?token=SUPERSECRET")
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert await hook.aclose() == 1
    err = capsys.readouterr().err
    assert "SUPERSECRET" not in err
    assert "token=***" in err


def test_notify_rejects_both_backends() -> None:
    hook = NotifyHook()
    problems = hook.configure({"url": "http://example/notify", "command": "true"})
    assert len(problems) == 1
    assert "exactly one" in problems[0]


def test_notify_rejects_both_backends_at_construction() -> None:
    with pytest.raises(ValueError, match="exactly one of url or command"):
        NotifyHook(url="http://example/notify", command="true")


def test_notify_rejects_an_empty_event_list() -> None:
    hook = NotifyHook()
    problems = hook.configure({"events": []})
    assert len(problems) == 1
    assert "at least one" in problems[0]
    assert hook.events == ["on_finish"]
