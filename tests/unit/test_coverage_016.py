"""Coverage for the 0.1.6 error and discovery paths.

Not "coverage for its own sake": each test here pins a branch whose absence
would turn a configuration mistake into a silent no-op or a traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from downctl import app
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.credentials import AuthError, AuthMaterial, AuthResolver, build_rules
from downctl.core.errors import (
    FAILURE_IO,
    FAILURE_PERMANENT,
    FAILURE_TIMEOUT,
    FAILURE_TRANSIENT_EXHAUSTED,
    DownctlError,
    TransientHTTPError,
    classify_failure,
)
from downctl.core.job import Job
from downctl.core.locks import LockError
from downctl.plugin import loader
from downctl.plugin.hooks import HookTasks
from downctl.plugin.registry import PluginError, Registry
from downctl.plugin.scaffold import scaffold
from downctl.plugin.testing import (
    assert_registers,
    capabilities,
    check_plugin,
    fake_context,
    option_names,
)


class _Provider:
    name = "fake"
    api_version = "1"
    priority = 0

    def __init__(self, value: object = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def can_handle(self, host: str) -> bool:
        return True

    async def material(self, host: str, ctx: RuntimeContext, options: dict[str, object]):
        if self.error is not None:
            raise self.error
        return self.value


class _Registry:
    def __init__(self, provider: object | None) -> None:
        self.provider = provider

    def get(self, category: str, name: str) -> object | None:
        return self.provider if category == "auth" and name == "fake" else None

    def names(self, category: str) -> list[str]:
        return ["fake"] if category == "auth" else []


def _ctx() -> RuntimeContext:
    return RuntimeContext(session=object(), config=Config())  # type: ignore[arg-type]


def test_an_auth_table_must_be_a_mapping() -> None:
    with pytest.raises(DownctlError, match="must be a table"):
        build_rules({"example.test": "nope"})


def test_an_auth_rule_needs_a_provider() -> None:
    with pytest.raises(DownctlError, match='needs a non-empty "provider"'):
        build_rules({"example.test": {"env": "TOKEN"}})


@pytest.mark.parametrize("ttl", ["soon", -1])
async def test_a_bad_ttl_is_refused(ttl: object) -> None:
    resolver = AuthResolver(
        build_rules({"example.test": {"provider": "fake", "ttl": ttl}}),
        _Registry(_Provider(AuthMaterial())),  # type: ignore[arg-type]
        _ctx(),
    )
    with pytest.raises(DownctlError, match="ttl"):
        await resolver.material_for("https://example.test/x")


async def test_a_provider_returning_the_wrong_type_is_named() -> None:
    resolver = AuthResolver(
        build_rules({"example.test": {"provider": "fake"}}),
        _Registry(_Provider("not-material")),  # type: ignore[arg-type]
        _ctx(),
    )
    with pytest.raises(AuthError, match="expected AuthMaterial"):
        await resolver.material_for("https://example.test/x")


async def test_an_unknown_provider_lists_the_available_ones() -> None:
    resolver = AuthResolver(
        build_rules({"example.test": {"provider": "missing"}}),
        _Registry(None),  # type: ignore[arg-type]
        _ctx(),
    )
    with pytest.raises(AuthError, match="unknown auth provider 'missing'"):
        await resolver.material_for("https://example.test/x")


def test_a_wildcard_does_not_match_its_apex() -> None:
    rules = build_rules({"*.corp.example": {"provider": "fake"}})
    resolver = AuthResolver(rules, _Registry(_Provider()), _ctx())  # type: ignore[arg-type]
    assert resolver.rule_for("a.corp.example") is not None
    assert resolver.rule_for("corp.example") is None
    assert resolver.rule_for("example.test") is None


def test_auth_material_describes_basic_without_the_value() -> None:
    material = AuthMaterial(basic=("user", "pass"), source="netrc")
    assert material.describe() == "basic (from netrc)"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("slow"), FAILURE_TIMEOUT),
        (OSError("no space"), FAILURE_IO),
        (LockError("held"), FAILURE_PERMANENT),
        (TransientHTTPError(503, "u"), FAILURE_TRANSIENT_EXHAUSTED),
        (DownctlError("other"), FAILURE_PERMANENT),
    ],
)
def test_failure_classes_are_decided_once(exc: BaseException, expected: str) -> None:
    assert classify_failure(exc) == expected


def test_a_job_without_a_name_still_has_a_label() -> None:
    assert Job(name="", targets=["x"]).label() == "<unnamed>"
    assert Job(name="a", targets=["x"]).label() == "a"


def test_load_target_reads_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = loader.load_target("downctl.builtin.auths.env_auth")
    assert [label for _category, label, _plugin in entries] == ["EnvAuth"]


def test_load_target_reads_a_distribution_without_plugins() -> None:
    assert loader.load_target("downctl") == []


def test_load_target_rejects_an_unknown_name() -> None:
    from downctl.plugin.registry import PluginError

    with pytest.raises(PluginError, match="not importable"):
        loader.load_target("downctl_does_not_exist_xyz")


def test_retired_groups_are_reported(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class _EP:
        name = "old"

    monkeypatch.setattr(
        loader.metadata,
        "entry_points",
        lambda group=None, _EP=_EP: [_EP()] if group == "downctl.sinks" else [],
    )
    loader.warn_retired_groups()
    assert "downctl.sinks" in capsys.readouterr().err


def test_testing_helpers_describe_a_plugin() -> None:
    from downctl.builtin.hooks.checksum_hook import ChecksumHook
    from downctl.builtin.transports.http_transport import HTTPTransport

    transport = HTTPTransport()
    assert "sends_content_range" in capabilities(transport, "transport")
    assert option_names(ChecksumHook()) == ["algo", "events"]
    assert option_names(HTTPTransport()) == []
    assert check_plugin(transport, "transport") == []
    context = fake_context()
    assert context.job == ""
    assert context.params == {}


def test_assert_registers_reports_a_bad_plugin() -> None:
    class _Broken:
        name = "broken"
        priority = 0

        def can_handle(self, target: str) -> bool:
            return False

        async def resolve(self, target: str, ctx: RuntimeContext) -> list[object]:
            return []

    with pytest.raises(PluginError, match="api_version"):
        assert_registers(Registry(), "source", _Broken())


def test_scaffold_refuses_a_bad_kind_a_bad_name_and_a_full_directory(tmp_path: Path) -> None:
    with pytest.raises(DownctlError, match="unknown plugin kind"):
        scaffold("demo", "nope", str(tmp_path))
    with pytest.raises(DownctlError, match="must be lower-case"):
        scaffold("Demo!", "source", str(tmp_path))
    (tmp_path / "demo_plugin").mkdir()
    (tmp_path / "demo_plugin" / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(DownctlError, match="non-empty"):
        scaffold("demo", "source", str(tmp_path))


async def test_hook_tasks_report_a_finished_thread() -> None:
    tasks = HookTasks("demo")
    assert tasks.spawn_thread(lambda: None) is True
    assert await tasks.aclose() == 0


# --------------------------------------------------------------------------- #
# --retry-failed and the header names it now records (R-1)
# --------------------------------------------------------------------------- #
def _failed_record_with_header() -> dict[str, object]:
    from downctl.core.task import DownloadTask

    task = DownloadTask(url="https://h/f", target="f.bin", headers={"X-Api-Key": "SECRET"}, size=1)
    _kind, payload = app._parse_failed_line(app._fmt_url(task, FAILURE_PERMANENT))
    assert isinstance(payload, dict)
    return payload


def _retry_context(tmp_path: Path) -> RuntimeContext:
    from downctl.plugin.loader import discover

    return RuntimeContext(
        session=object(),  # type: ignore[arg-type]
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        registry=discover(),
    )


async def test_a_retry_without_the_recorded_header_says_which_one(
    tmp_path: Path,
) -> None:
    """Replaying without the credential produced a 401 that blamed the server."""
    payload = _failed_record_with_header()
    ctx = _retry_context(tmp_path)
    args = app._default_get_args()
    args.targets = ["x"]

    with pytest.raises(DownctlError, match="X-Api-Key"):
        await app._retry_task(payload, args, ctx.config, ctx)


async def test_a_retry_with_the_header_resupplied_attaches_it(tmp_path: Path) -> None:
    payload = _failed_record_with_header()
    ctx = _retry_context(tmp_path)
    args = app._default_get_args()
    args.targets = ["x"]
    args.header = ["X-Api-Key: SECRET"]

    task = await app._retry_task(payload, args, ctx.config, ctx)
    assert task.headers == {"X-Api-Key": "SECRET"}
    assert "SECRET" not in str(payload)  # the log never stored the value
