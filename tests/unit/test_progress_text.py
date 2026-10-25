"""Cell-accurate text helpers for the progress display."""

from __future__ import annotations

from downctl.progress import text as T


def test_display_width_counts_cells_not_bytes() -> None:
    assert T.display_width("abc") == 3
    assert T.display_width("文件") == 4
    assert T.display_width("a文") == 3
    assert T.display_width("e\u0301") == 1  # combining acute occupies no cell
    assert T.display_width("\x1b[31mred\x1b[0m") == 3


def test_ellipsize_never_exceeds_the_budget() -> None:
    assert T.ellipsize("hello", 10) == "hello"
    assert T.ellipsize("hello", 5) == "hello"
    assert T.display_width(T.ellipsize("hello world", 6)) <= 6
    assert T.display_width(T.ellipsize("很长的文件名.bin", 7)) <= 7


def test_ellipsize_middle_keeps_both_ends() -> None:
    result = T.ellipsize_middle("abcdefghijkl.bin", 9)
    assert result.startswith("a")
    assert result.endswith("l.bin") or result.endswith(".bin")
    assert T.display_width(result) <= 9


def test_shorten_path_keeps_the_basename() -> None:
    assert T.shorten_path("/very/long/prefix/model.bin", 40) == "/very/long/prefix/model.bin"
    shortened = T.shorten_path("/very/long/prefix/model.bin", 14)
    assert "model.bin" in shortened
    assert T.display_width(shortened) <= 14
    middle = T.shorten_path("a_very_long_filename_that_does_not_fit.bin", 16)
    assert T.display_width(middle) <= 16
    assert middle.endswith(".bin")


def test_fit_pads_and_truncates() -> None:
    assert T.fit("abc", 5) == "abc  "
    assert T.fit("abc", 5, align="right") == "  abc"
    assert T.fit("abc", 5, align="center") == " abc "
    assert T.fit("abcdef", 3) == "a…" or T.display_width(T.fit("abcdef", 3)) <= 3


def test_human_rate_and_durations() -> None:
    assert T.format_rate(None) == "--"
    assert T.format_rate(0) == "--"
    assert T.format_rate(1 << 20) == "1.0 MB/s"
    assert T.format_eta(None) == "--:--"
    assert T.format_eta(0) == "00:00"
    assert T.format_eta(65.4) == "01:05"
    assert T.format_eta(3725) == "1:02:05"
    assert T.format_elapsed(4.9) == "4s"
    assert T.format_elapsed(65) == "1m 05s"
    assert T.format_elapsed(3700) == "1h 01m"
    assert T.format_percent(None) == "--%"
    assert T.format_percent(0.456) == " 45%"
    assert T.format_percent(1.5) == "100%"


def test_bars_have_a_fixed_width() -> None:
    assert len(T.make_bar(0.5, 10).text) == 10
    assert T.make_bar(0.0, 10).filled == ""
    assert T.make_bar(1.0, 10).empty == ""
    assert len(T.make_bar(None, 10).text) == 10
    assert T.make_bar(None, 10).filled == ""
    ascii_bar = T.make_bar(0.5, 10, unicode=False)
    assert ascii_bar.text == "#####-----"
    assert len(T.make_bar(0.999, 10).text) == 10


def test_paint_only_when_enabled() -> None:
    assert T.paint("x", T.RED, False) == "x"
    assert T.paint("x", T.RED, True) == f"{T.RED}x{T.RESET}"


def test_spinner_has_the_right_alphabet() -> None:
    assert T.spinner(0) in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert T.spinner(1, unicode=False) in "|/-\\"
    assert T.spinner(999, unicode=False) in "|/-\\"
