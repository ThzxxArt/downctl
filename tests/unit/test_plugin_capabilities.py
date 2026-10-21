"""A capability flag is a promise only when it is ``True``.

``capabilities()`` used ``isinstance`` against ``runtime_checkable`` Protocols,
which check that an attribute *exists*, not what it contains. The plain-URL
source declares ``supports_filters = False`` and was still reported beside the
source that really can filter.
"""

from __future__ import annotations

from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.sources.http_index_source import HTTPIndexSource
from downctl.builtin.sources.url_source import URLSource
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.plugin.testing import capabilities


class _NoFilters:
    supports_filters = False


class _Filters:
    supports_filters = True


class _HumanReporter:
    machine_readable = False


class _HumanTransport:
    sends_content_range = False


class _Closing:
    async def aclose(self) -> int:
        return 0


class _NotClosing:
    aclose = True


def test_a_false_flag_is_not_reported_as_supported() -> None:
    assert capabilities(_NoFilters(), "source") == []
    assert capabilities(_HumanReporter(), "reporter") == []
    assert capabilities(_HumanTransport(), "transport") == []
    assert capabilities(_NotClosing(), "hook") == []


def test_a_true_flag_is_still_reported() -> None:
    assert capabilities(_Filters(), "source") == ["supports_filters"]
    assert capabilities(_Closing(), "hook") == ["aclose"]


def test_the_builtins_report_what_they_actually_do() -> None:
    # ``url`` has no listing to filter; ``http_index`` does.
    assert "supports_filters" not in capabilities(URLSource(), "source")
    assert "supports_filters" in capabilities(HTTPIndexSource(), "source")
    assert "machine_readable" in capabilities(JSONReporter(), "reporter")
    assert "sends_content_range" in capabilities(HTTPTransport(), "transport")
