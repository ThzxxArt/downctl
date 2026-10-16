"""Event bus tests."""

from __future__ import annotations

from downctl.plugin.hooks import EventBus


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
