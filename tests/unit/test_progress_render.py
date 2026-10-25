"""Layout tests for the live progress frame.

The renderer is a pure function, so these assertions can pin what a user sees
without a pseudo-terminal. Width is checked on the *plain* line (escapes
removed) for every supported width.
"""

from __future__ import annotations

from downctl.core.task import DownloadTask
from downctl.progress import text as T
from downctl.progress.model import RunView
from downctl.progress.render import RenderOptions, render

MB = 1 << 20


def _task(name: str, size: int = 12 * MB) -> DownloadTask:
    return DownloadTask(url=f"https://example.test/{name}", target=name, size=size)


def _view(tasks: list[DownloadTask], *, jobs: int = 3) -> RunView:
    view = RunView()
    view.start(len(tasks), sum(task.size for task in tasks), jobs, now=0.0)
    for task in tasks:
        view.task_started(task, 0.0)
    return view


def _plain(lines: list[str]) -> list[str]:
    return [T.strip_ansi(line) for line in lines]


def _assert_width(lines: list[str], width: int) -> None:
    for line in _plain(lines):
        assert T.display_width(line) <= width, f"{line!r} exceeds {width} columns"


def test_multi_file_frame_has_a_header_a_bar_and_one_row_per_worker() -> None:
    first, second, third = (
        _task("file1.bin"),
        _task("file2.bin"),
        _task("dir/file3.bin"),
    )
    view = _view([first, second, third])
    view.task_progress(first, 6 * MB, 1.0)
    view.task_progress(second, 3 * MB, 1.0)
    view.task_retrying(third, 2, 5.0, "HTTP 502", 1.0)
    lines = _plain(render(view, RenderOptions(width=100, height=12, now=1.0)))
    assert "downloading" in lines[0]
    assert "3 files" in lines[0]
    assert "done" in lines[1]
    assert any("retrying in" in line for line in lines)
    assert any("file1.bin" in line for line in lines)
    assert any("file3.bin" in line for line in lines)


def test_retry_row_comes_before_active_rows() -> None:
    first, second = _task("slow.bin"), _task("fast.bin")
    view = _view([first, second])
    view.task_progress(first, 1 * MB, 1.0)
    view.task_progress(second, 2 * MB, 1.0)
    view.task_retrying(first, 2, 5.0, "HTTP 502", 1.0)
    lines = _plain(render(view, RenderOptions(width=100, height=12, now=1.0)))
    rows = [line for line in lines if "retrying" in line or "fast.bin" in line]
    assert "retrying" in rows[0]


def test_single_file_does_not_draw_two_bars_for_the_same_numbers() -> None:
    task = _task("file1.bin")
    view = _view([task])
    view.task_progress(task, 6 * MB, 1.0)
    lines = _plain(render(view, RenderOptions(width=100, height=8, now=1.0)))
    assert lines[0].startswith("downloading file1.bin")
    assert sum(line.count("[") for line in lines) == 1, lines
    assert "50%" in lines[1]


def test_one_file_before_it_starts_does_not_show_a_false_zero_bar() -> None:
    view = RunView()
    view.start(1, 12 * MB, 1, now=0.0)
    lines = _plain(render(view, RenderOptions(width=80, height=8, now=0.0)))
    assert len(lines) == 1
    assert "0%" not in lines[0]
    assert "12.0 MB" in lines[0]


def test_unknown_size_never_invents_a_percentage() -> None:
    task = _task("stream.bin", 0)
    view = _view([task])
    view.unknown_tasks = 1
    view.task_progress(task, 2 * MB, 1.0)
    lines = _plain(render(view, RenderOptions(width=80, height=8, now=1.0)))
    assert "size unknown" in lines[1]
    assert "%" not in "".join(lines)


def test_probe_phase_shows_count_and_bar() -> None:
    view = RunView()
    view.set_phase("probe", 3, 10)
    lines = _plain(render(view, RenderOptions(width=80, height=8, now=1.0)))
    assert "preparing 3/10 targets" in lines[0]
    assert "30%" in lines[1]


def test_every_width_keeps_every_line_inside_the_terminal() -> None:
    tasks = [_task(f"file{i}.bin") for i in range(4)]
    view = _view(tasks)
    for index, task in enumerate(tasks):
        view.task_progress(task, (index + 1) * MB, 1.0)
    view.task_retrying(tasks[0], 2, 5.0, "HTTP 502 with a long explanation", 1.0)
    for width in (24, 32, 40, 56, 64, 80, 100, 140, 200):
        lines = render(view, RenderOptions(width=width, height=10, now=1.0))
        assert lines
        _assert_width(lines, max(24, width))


def test_ascii_mode_uses_ascii_markers_and_bar_cells() -> None:
    task = _task("file1.bin")
    view = _view([task])
    view.task_progress(task, 6 * MB, 1.0)
    lines = _plain(render(view, RenderOptions(width=80, height=8, unicode=False, now=1.0)))
    joined = "\n".join(lines)
    assert "█" not in joined
    assert "░" not in joined
    assert "▶" not in joined
    assert "#" in joined or "-" in joined


def test_colour_adds_escapes_without_changing_visible_width() -> None:
    task = _task("file1.bin")
    view = _view([task])
    view.task_progress(task, 6 * MB, 1.0)
    plain = render(view, RenderOptions(width=80, height=8, color=False, now=1.0))
    coloured = render(view, RenderOptions(width=80, height=8, color=True, now=1.0))
    assert "\x1b[" in "\n".join(coloured)
    assert [T.display_width(line) for line in coloured] == [T.display_width(line) for line in plain]


def test_duplicate_basenames_are_disambiguated() -> None:
    first = _task("a/data.bin")
    second = _task("b/data.bin")
    view = _view([first, second])
    view.task_progress(first, 1 * MB, 1.0)
    view.task_progress(second, 1 * MB, 1.0)
    lines = _plain(render(view, RenderOptions(width=80, height=8, now=1.0)))
    assert any("a/data.bin" in line for line in lines)
    assert any("b/data.bin" in line for line in lines)


def test_height_folds_extra_active_rows() -> None:
    tasks = [_task(f"file{i}.bin") for i in range(6)]
    view = _view(tasks)
    for task in tasks:
        view.task_progress(task, 1 * MB, 1.0)
    lines = _plain(render(view, RenderOptions(width=90, height=6, now=1.0)))
    assert any("more active" in line for line in lines)
    assert len(lines) <= 6


def test_failed_file_stays_visible_for_the_rest_of_the_run() -> None:
    first, second = _task("bad.bin"), _task("good.bin")
    view = _view([first, second])
    view.task_progress(second, 2 * MB, 1.0)
    view.task_finished(first, False, 1.0)
    lines = _plain(render(view, RenderOptions(width=90, height=8, now=1.0)))
    assert any("bad.bin" in line for line in lines)
    assert any("failed" in line or "✗" in line for line in lines)


def test_verifying_row_explains_the_hash_phase() -> None:
    task = _task("image.iso")
    view = _view([task])
    view.task_progress(task, 12 * MB, 1.0)
    view.task_phase(task, "verify", 1.5, "sha256")
    lines = _plain(render(view, RenderOptions(width=90, height=8, now=1.5)))
    assert any("verifying" in line for line in lines)
    assert any("sha256" in line for line in lines)
