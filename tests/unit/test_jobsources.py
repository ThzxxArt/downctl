"""Tests for the built-in TOML and JSON job-file sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from downctl.builtin.jobsources import JsonJobSource, TomlJobSource
from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.errors import DownctlError
from downctl.plugin.protocols import API_VERSION, JobSource

_TOML_EXAMPLE = """\
version = 1

[[job]]
name = "llama-3-8b"
targets = ["hf://meta-llama/Llama-3-8B@main"]
dir = "models/llama"
include = ["*.safetensors", "*.json"]
checksum_manifest = "checksums.txt"

[[job]]
name = "tool"
targets = ["https://example.com/tool.tar.gz"]
checksum = "sha256:9f2c..."
hooks = ["archive"]
"""


@pytest.fixture
def ctx() -> RuntimeContext:
    # ``load`` does not consult the context; the plugin only reads the file.
    return RuntimeContext(session=None, config=Config())  # type: ignore[arg-type]


def _write_toml(tmp_path: Path, body: str, name: str = "jobs.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_json(tmp_path: Path, document: object, name: str = "jobs.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


async def test_toml_loads_both_jobs_end_to_end(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_toml(tmp_path, _TOML_EXAMPLE)

    jobs = await TomlJobSource().load(str(path), ctx)

    assert [job.name for job in jobs] == ["llama-3-8b", "tool"]
    llama, tool = jobs
    assert llama.targets == ["hf://meta-llama/Llama-3-8B@main"]
    assert llama.options["dir"] == str(tmp_path / "models" / "llama")
    assert llama.options["include"] == ["*.safetensors", "*.json"]
    assert llama.options["checksums"] == str(tmp_path / "checksums.txt")
    assert tool.options["checksum"] == "sha256:9f2c..."
    assert tool.options["hook"] == ["archive"]
    assert llama.origin == str(path)
    assert tool.origin == str(path)


async def test_json_loads_end_to_end_with_default_names(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    absolute = str(tmp_path / "absolute")
    path = _write_json(
        tmp_path,
        {
            "version": 1,
            "jobs": [
                {
                    "targets": "https://example.com/a.bin",
                    "dir": absolute,
                    "headers": ["X-Test: 1"],
                    "continue": True,
                    "jobs": 4,
                },
                {
                    "name": "second",
                    "targets": [
                        "https://example.com/b.bin",
                        "https://example.com/c.bin",
                    ],
                },
            ],
        },
    )

    jobs = await JsonJobSource().load(str(path), ctx)

    assert [job.name for job in jobs] == ["job-1", "second"]
    assert jobs[0].targets == ["https://example.com/a.bin"]
    assert jobs[0].options["dir"] == absolute
    assert jobs[0].options["header"] == ["X-Test: 1"]
    assert jobs[0].options["continue_"] is True
    assert jobs[0].options["jobs"] == 4
    assert jobs[1].targets == [
        "https://example.com/b.bin",
        "https://example.com/c.bin",
    ]
    assert all(job.origin == str(path) for job in jobs)


@pytest.mark.parametrize(
    ("source", "path", "expected"),
    [
        (TomlJobSource(), "jobs.toml", True),
        (TomlJobSource(), "JOBS.TML", True),
        (TomlJobSource(), "jobs.json", False),
        (TomlJobSource(), "jobs.toml.bak", False),
        (JsonJobSource(), "jobs.JSON", True),
        (JsonJobSource(), "jobs.toml", False),
        (JsonJobSource(), "myjson", False),
    ],
)
def test_can_handle_matches_a_case_insensitive_suffix(
    source: object, path: str, expected: bool
) -> None:
    assert source.can_handle(path) is expected  # type: ignore[attr-defined]


def test_both_sources_satisfy_the_job_source_protocol() -> None:
    assert isinstance(TomlJobSource(), JobSource)
    assert isinstance(JsonJobSource(), JobSource)
    assert TomlJobSource.api_version == API_VERSION
    assert JsonJobSource.api_version == API_VERSION
    assert TomlJobSource.formats == ("toml", "tml")
    assert JsonJobSource.formats == ("json",)


async def test_version_two_is_refused_as_written_by_a_newer_downctl(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    toml_path = _write_toml(tmp_path, "version = 2\n")
    with pytest.raises(DownctlError, match="newer downctl") as toml_error:
        await TomlJobSource().load(str(toml_path), ctx)
    assert str(toml_path) in str(toml_error.value)

    json_path = _write_json(tmp_path, {"version": 2, "jobs": []})
    with pytest.raises(DownctlError, match="newer downctl") as json_error:
        await JsonJobSource().load(str(json_path), ctx)
    assert str(json_path) in str(json_error.value)


async def test_version_is_required_and_must_be_one(tmp_path: Path, ctx: RuntimeContext) -> None:
    missing = _write_toml(tmp_path, '[[job]]\ntargets = "https://example.com/x"\n')
    with pytest.raises(DownctlError) as missing_error:
        await TomlJobSource().load(str(missing), ctx)
    missing_message = str(missing_error.value)
    assert str(missing) in missing_message
    assert "'version'" in missing_message
    assert "supported: 1" in missing_message

    wrong = _write_json(tmp_path, {"version": "1", "jobs": [{"targets": "x"}]})
    with pytest.raises(DownctlError, match="supported: 1") as wrong_error:
        await JsonJobSource().load(str(wrong), ctx)
    assert str(wrong) in str(wrong_error.value)


async def test_unknown_option_lists_the_allowed_keys(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
name = "x"
targets = ["https://example.com/x"]
colour = "red"
""",
    )

    with pytest.raises(DownctlError) as info:
        await TomlJobSource().load(str(path), ctx)

    message = str(info.value)
    assert str(path) in message
    assert "job #1 'x'" in message
    assert "unknown option 'colour'" in message
    assert "allowed:" in message
    for key in ("dir", "jobs", "checksums", "hook", "continue_", "output"):
        assert key in message


async def test_refused_option_reports_the_reason(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = ["https://example.com/x"]
retry_failed = true
""",
    )

    with pytest.raises(DownctlError, match="the failed log is per destination directory") as info:
        await TomlJobSource().load(str(path), ctx)

    message = str(info.value)
    assert str(path) in message
    assert "job #1" in message
    assert "retry_failed" in message


async def test_duplicate_job_names_are_refused(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
name = "same"
targets = "https://example.com/a"
[[job]]
name = "same"
targets = "https://example.com/b"
""",
    )

    with pytest.raises(DownctlError, match="duplicate job name 'same'") as info:
        await TomlJobSource().load(str(path), ctx)

    message = str(info.value)
    assert str(path) in message
    assert "#1" in message
    assert "#2" in message


async def test_aliases_are_normalized_to_the_cli_option_names(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    checksums = str(tmp_path / "checksums.txt")
    path = _write_toml(
        tmp_path,
        f"""\
version = 1
[[job]]
name = "aliased"
targets = ["https://example.com/a"]
checksum_manifest = {json.dumps(checksums)}
hooks = ["archive"]
headers = ["X-Test: yes"]
continue = true
""",
    )

    job = (await TomlJobSource().load(str(path), ctx))[0]

    assert job.options["checksums"] == checksums
    assert job.options["hook"] == ["archive"]
    assert job.options["header"] == ["X-Test: yes"]
    assert job.options["continue_"] is True
    for alias in ("checksum_manifest", "hooks", "headers", "continue"):
        assert alias not in job.options

    duplicate = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = "https://example.com/x"
hooks = ["archive"]
hook = "logfile"
""",
        name="duplicate-option.toml",
    )
    with pytest.raises(DownctlError, match="option 'hook' given twice"):
        await TomlJobSource().load(str(duplicate), ctx)


async def test_a_single_string_becomes_a_one_element_list(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = "https://example.com/one"
include = "*.json"
exclude = "*.tmp"
hook = "archive"
header = "X-One: 1"
""",
    )

    job = (await TomlJobSource().load(str(path), ctx))[0]

    assert job.targets == ["https://example.com/one"]
    assert job.options["include"] == ["*.json"]
    assert job.options["exclude"] == ["*.tmp"]
    assert job.options["hook"] == ["archive"]
    assert job.options["header"] == ["X-One: 1"]


async def test_origin_is_absolute_even_for_a_relative_path(
    tmp_path: Path, ctx: RuntimeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = "https://example.com/x"
""",
    )
    monkeypatch.chdir(tmp_path)

    jobs = await TomlJobSource().load("jobs.toml", ctx)

    assert jobs[0].origin == str(path)


async def test_relative_paths_are_resolved_against_the_job_file(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    nested = tmp_path / "configs"
    nested.mkdir()
    path = nested / "jobs.toml"
    path.write_text(
        """\
version = 1
[[job]]
targets = "https://example.com/x"
dir = "../models"
checksums = "runs/checksums.txt"
""",
        encoding="utf-8",
    )

    job = (await TomlJobSource().load(str(path), ctx))[0]

    # ``../models`` is relative to the file, so the file can be moved with the
    # tree it describes; ``dir`` is normalized but still under the file's parent.
    assert job.options["dir"] == str(tmp_path / "models")
    assert job.options["checksums"] == str(nested / "runs" / "checksums.txt")
    assert job.origin == str(path)


async def test_a_job_must_name_at_least_one_target(tmp_path: Path, ctx: RuntimeContext) -> None:
    missing = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
name = "empty"
""",
    )
    with pytest.raises(DownctlError, match="missing required 'targets'") as info:
        await TomlJobSource().load(str(missing), ctx)
    assert str(missing) in str(info.value)
    assert "job #1 'empty'" in str(info.value)

    blank = _write_json(
        tmp_path,
        {"version": 1, "jobs": [{"name": "blank", "targets": []}]},
        name="blank.json",
    )
    with pytest.raises(DownctlError, match="at least one non-empty target"):
        await JsonJobSource().load(str(blank), ctx)


async def test_boolean_options_are_not_coerced(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_json(
        tmp_path,
        {
            "version": 1,
            "jobs": [
                {
                    "name": "strict",
                    "targets": "https://example.com/x",
                    "force": "false",
                }
            ],
        },
    )

    with pytest.raises(DownctlError) as info:
        await JsonJobSource().load(str(path), ctx)

    message = str(info.value)
    assert str(path) in message
    assert "job #1 'strict'" in message
    assert "option 'force' must be a boolean" in message
    assert "the string 'false'" in message

    number = _write_json(
        tmp_path,
        {
            "version": 1,
            "jobs": [{"targets": "https://example.com/x", "skip_existing": 0}],
        },
        name="numbers.json",
    )
    with pytest.raises(DownctlError, match="must be a boolean"):
        await JsonJobSource().load(str(number), ctx)


async def test_integer_options_are_not_booleans_and_jobs_stays_positive(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    zero = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = "https://example.com/x"
jobs = 0
""",
    )
    with pytest.raises(DownctlError, match="positive integer"):
        await TomlJobSource().load(str(zero), ctx)

    boolean = _write_json(
        tmp_path,
        {
            "version": 1,
            "jobs": [{"targets": "https://example.com/x", "retries": True}],
        },
        name="boolean.json",
    )
    with pytest.raises(DownctlError, match="'retries' must be an integer") as info:
        await JsonJobSource().load(str(boolean), ctx)
    assert "the boolean True" in str(info.value)

    string = _write_json(
        tmp_path,
        {
            "version": 1,
            "jobs": [{"targets": "https://example.com/x", "jobs": "4"}],
        },
        name="string.json",
    )
    with pytest.raises(DownctlError, match="positive integer"):
        await JsonJobSource().load(str(string), ctx)


async def test_json_accepts_job_as_an_alias_for_jobs(tmp_path: Path, ctx: RuntimeContext) -> None:
    path = _write_json(
        tmp_path,
        {
            "version": 1,
            "job": [{"name": "one", "targets": "https://example.com/one"}],
        },
    )

    jobs = await JsonJobSource().load(str(path), ctx)

    assert len(jobs) == 1
    assert jobs[0].name == "one"
    assert jobs[0].targets == ["https://example.com/one"]

    both = _write_json(tmp_path, {"version": 1, "jobs": [], "job": []}, name="both.json")
    with pytest.raises(DownctlError, match="either 'jobs' or 'job'"):
        await JsonJobSource().load(str(both), ctx)


async def test_a_file_without_jobs_is_refused(tmp_path: Path, ctx: RuntimeContext) -> None:
    toml_path = _write_toml(tmp_path, "version = 1\n")
    with pytest.raises(DownctlError, match="no jobs defined") as info:
        await TomlJobSource().load(str(toml_path), ctx)
    message = str(info.value)
    assert str(toml_path) in message
    assert "[[job]]" in message

    json_path = _write_json(tmp_path, {"version": 1, "jobs": []}, name="empty.json")
    with pytest.raises(DownctlError, match="no jobs defined") as info:
        await JsonJobSource().load(str(json_path), ctx)
    assert '"jobs"' in str(info.value)


async def test_a_missing_file_is_reported_with_its_path(
    tmp_path: Path, ctx: RuntimeContext
) -> None:
    missing_toml = tmp_path / "absent.toml"
    with pytest.raises(DownctlError, match="cannot read job file") as info:
        await TomlJobSource().load(str(missing_toml), ctx)
    assert str(missing_toml) in str(info.value)

    missing_json = tmp_path / "absent.json"
    with pytest.raises(DownctlError, match="cannot read job file"):
        await JsonJobSource().load(str(missing_json), ctx)


async def test_malformed_files_name_the_file(tmp_path: Path, ctx: RuntimeContext) -> None:
    toml_path = _write_toml(tmp_path, "this is not = toml =\n")
    with pytest.raises(DownctlError, match="invalid TOML") as info:
        await TomlJobSource().load(str(toml_path), ctx)
    assert str(toml_path) in str(info.value)

    json_path = tmp_path / "broken.json"
    json_path.write_text('{"version": 1,', encoding="utf-8")
    with pytest.raises(DownctlError, match="invalid JSON") as info:
        await JsonJobSource().load(str(json_path), ctx)
    assert str(json_path) in str(info.value)


async def test_no_proxy_is_a_job_option(tmp_path: Path, ctx: RuntimeContext) -> None:
    """``--no-proxy`` is a download option, so a job may say it too.

    The docs promise every download option is available per job, but
    ``no_proxy`` was absent from the allowlist (and from the boolean type set),
    so the file was refused with "unknown option".
    """
    path = _write_toml(
        tmp_path,
        """\
version = 1
[[job]]
targets = ["https://example.com/x"]
no_proxy = true
""",
    )
    jobs = await TomlJobSource().load(str(path), ctx)
    assert jobs[0].options == {"no_proxy": True}
