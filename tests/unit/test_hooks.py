"""Event bus tests."""

from __future__ import annotations

from downctl.plugin.hooks import EventBus, HookTasks


def test_event_bus_dispatch_and_isolation() -> None:
    bus = EventBus()
    received: list[str] = []

    def ok_handler(payload: dict[str, object]) -> None:
        received.append("ok")

    def bad_handler(payload: dict[str, object]) -> None:
        raise RuntimeError("boom")

    bus.subscribe("on_start", ok_handler)
    bus.subscribe("on_start", bad_handler)
    bus.subscribe("on_start", ok_handler)

    bus.emit("on_start", {"targets": []})
    assert received == ["ok", "ok"]


def test_event_bus_registers_hook_plugin() -> None:
    import tempfile
    from pathlib import Path

    from downctl.builtin.hooks.logfile_hook import LogfileHook

    bus = EventBus()
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "run.log"
        bus.register_hook(LogfileHook(str(log)))
        bus.emit("on_finish", {"succeeded": 1, "failed": 0})
        assert log.is_file()
        assert "on_finish" in log.read_text(encoding="utf-8")


def test_hook_error_text_is_redacted(capsys) -> None:
    bus = EventBus()

    def bad_handler(payload: dict[str, object]) -> None:
        raise RuntimeError("failed at http://h/x?token=SUPERSECRET")

    bus.subscribe("on_start", bad_handler)
    bus.emit("on_start", {"targets": []})
    err = capsys.readouterr().err
    assert "SUPERSECRET" not in err
    assert "token=***" in err


# --------------------------------------------------------------------------- #
# a hook failure is contained, but never invisible
# --------------------------------------------------------------------------- #
def test_a_failing_hook_is_counted(capsys) -> None:
    bus = EventBus()

    def bad_handler(payload: dict[str, object]) -> None:
        raise RuntimeError("boom")

    bus.subscribe("on_start", bad_handler)
    bus.emit("on_start", {"targets": []})

    assert bus.failures == 1
    assert "hook error on on_start" in capsys.readouterr().err


def test_the_final_event_reports_how_many_hooks_failed() -> None:
    """``on_finish`` is where a machine reads the run summary."""
    bus = EventBus()

    def bad_handler(payload: dict[str, object]) -> None:
        raise RuntimeError("boom")

    bus.subscribe("on_start", bad_handler)
    bus.emit("on_start", {"targets": []})

    payload: dict[str, object] = {"succeeded": 1, "failed": 0}
    bus.emit("on_finish", payload)
    assert payload["hooks_failed"] == 1


async def test_a_hook_that_raises_in_a_thread_is_not_silent(capsys) -> None:
    """The task handle used to be dropped with its exception attached.

    ``add_done_callback(set.discard)`` forgot the task; whatever it raised went
    with it, so a post-processing step that broke produced no output at all.
    """
    tasks = HookTasks("demo")

    def boom() -> None:
        raise RuntimeError("timeout must be a number, not '300'")

    assert tasks.spawn_thread(boom)
    assert await tasks.aclose() == 1
    assert "timeout must be a number" in capsys.readouterr().err


async def test_a_hook_that_records_its_own_failure_is_counted() -> None:
    tasks = HookTasks("demo")
    tasks.record()
    tasks.record()
    assert await tasks.aclose() == 2


async def test_aclose_sums_what_each_hook_reported() -> None:
    class Reporting:
        def __init__(self, failures: int) -> None:
            self.failures = failures

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            return None

        async def aclose(self) -> int:
            return self.failures

    bus = EventBus()
    bus.register_hook(Reporting(1))  # type: ignore[arg-type]
    bus.register_hook(Reporting(2))  # type: ignore[arg-type]

    await bus.aclose()
    assert bus.failures == 3


async def test_a_raise_inside_aclose_is_counted_too(capsys) -> None:
    class Broken:
        def on_event(self, event: str, payload: dict[str, object]) -> None:
            return None

        async def aclose(self) -> None:
            raise RuntimeError("flush failed")

    bus = EventBus()
    bus.register_hook(Broken())  # type: ignore[arg-type]

    await bus.aclose()
    assert bus.failures == 1
    assert "hook shutdown error" in capsys.readouterr().err


async def test_a_broken_archive_is_reported_and_counted(tmp_path, capsys) -> None:
    """The concrete case: an archive that cannot be extracted.

    The extraction runs in a worker thread, so before this it could fail with no
    line of output and a zero exit code.
    """
    from downctl.builtin.hooks.archive_hook import ArchiveHook
    from downctl.core.task import DownloadTask

    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"this is not a zip file")
    task = DownloadTask(url="https://h/broken.zip", target="broken.zip")
    task.meta["path"] = str(broken)

    hook = ArchiveHook()
    hook.on_event("on_task_finish", {"task": task, "ok": True, "witness": "validator"})
    assert await hook.aclose() == 1
    assert "failed to extract" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# hook options are validated, not merely assigned
# --------------------------------------------------------------------------- #
def test_a_single_event_string_is_normalised_to_a_list() -> None:
    """``events = "on_finish"`` must not stay a string.

    As a string, ``event not in self.events`` becomes a *substring* test:
    ``"on_finish" in "on_task_finish"`` is true, so the hook fired on an event it
    was never configured for.
    """
    from downctl.builtin.hooks.checksum_hook import ChecksumHook

    hook = ChecksumHook()
    assert hook.configure({"events": "on_finish"}) == []
    assert hook.events == ["on_finish"]
    assert "on_task_finish" not in hook.events


def test_an_unknown_event_name_is_refused() -> None:
    from downctl.builtin.hooks.checksum_hook import ChecksumHook

    hook = ChecksumHook()
    problems = hook.configure({"events": ["on_finsh"]})
    assert problems and "unknown event" in problems[0]
    assert hook.events == ["on_task_finish"]


def test_an_unknown_option_names_the_ones_that_exist() -> None:
    from downctl.builtin.hooks.checksum_hook import ChecksumHook

    hook = ChecksumHook()
    problems = hook.configure({"evests": ["on_finish"]})
    assert problems and "unknown option 'evests'" in problems[0]
    assert "algo" in problems[0]


def test_a_value_of_the_wrong_type_is_refused() -> None:
    """``timeout = "300"`` used to reach ``subprocess.run`` and raise there."""
    from downctl.builtin.hooks.exec_hook import ExecHook

    hook = ExecHook()
    problems = hook.configure({"timeout": "300"})
    assert problems and "must be an integer" in problems[0]
    assert hook.timeout == 300

    assert hook.configure({"timeout": 60}) == []
    assert hook.timeout == 60


def test_a_third_party_hook_still_gets_unknown_keys_reported() -> None:
    """A hook without ``OPTIONS`` keeps the old assignment, plus a warning."""
    from downctl.plugin.hooks import apply_hook_options

    class ThirdParty:
        def __init__(self) -> None:
            self.url = ""

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            return None

    hook = ThirdParty()
    problems = apply_hook_options(hook, {"url": "https://h/x", "typo": 1})
    assert problems == ["unknown option 'typo'"]
    assert hook.url == "https://h/x"
