"""End-to-end smoke tests for the P1 skeleton."""

from __future__ import annotations

from downctl import API_VERSION, __version__
from downctl.builtin.sources.url_source import filename_from_url
from downctl.plugin.loader import discover


def test_version_and_api_version() -> None:
    assert __version__
    assert API_VERSION == "1"


def test_full_registry_and_source_resolution() -> None:
    registry = discover()
    assert len(registry) >= 9

    for name in ("url", "huggingface"):
        plugin = registry.get("source", name)
        assert plugin is not None
        assert plugin.api_version == API_VERSION


def test_filename_from_url() -> None:
    assert filename_from_url("https://example.com/a/b/model.safetensors") == "model.safetensors"
    assert filename_from_url("https://example.com/a/my%20file.bin?v=1") == "my file.bin"


def test_the_example_plugin_loads_the_documented_way(tmp_path) -> None:
    """The example is documentation that can be wrong.

    Its docstring used to say ``downctl --local-plugins ...``; the flag belongs
    to the subcommand, so the documented command exited 2 before loading
    anything. This runs the documented command for real.
    """
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "example_source.py"
    plugins = tmp_path / "downctl_plugins"
    plugins.mkdir()
    shutil.copy(source, plugins / "example_source.py")

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "DOWNCTL_CONFIG": str(tmp_path / "config.toml"),
        "NO_COLOR": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "downctl", "plugin", "list", "--local-plugins"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-400:]
    assert "example" in completed.stdout
