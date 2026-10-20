"""Where a proxy applies is decided per target, not per session.

A proxy that was *guessed* (the WSL host detection) says how this machine
reaches the network. It says nothing about where a particular URL lives, so a
request for this machine must not be handed to it: the proxy would resolve that
address on the other side of the boundary and reach a different service, or
nothing at all. Before this split, the guess was installed as the session-wide
proxy, so every download on such a host failed with a connection error that
named an address the user never configured.
"""

from __future__ import annotations

import pytest

from downctl import app
from downctl.config import Config, load_config
from downctl.session import PROXY_ALL, PROXY_EXTERNAL, ProxyPolicy

#: An address nothing listens on. Connecting to it fails immediately, so a test
#: that accidentally routes through it fails fast instead of timing out.
UNREACHABLE = "http://127.0.0.1:9"


async def test_a_loopback_target_is_not_routed_through_a_guessed_proxy(
    file_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WSL guess must not intercept a download from this machine.

    ``no_proxy`` is cleared explicitly: many hosts ship ``no_proxy=127.0.0.1``,
    and with it set this test would pass through the exclusion rather than
    through the local-host rule — which is exactly the kind of accidental green
    this release is about.
    """
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: UNREACHABLE)

    destination = tmp_path / "out"
    args = app._default_get_args()
    args.targets = [file_server.url("file")]
    args.dir = str(destination)
    args.reporter = "quiet"
    args.retries = 1

    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_an_explicit_proxy_still_applies_to_every_target(
    file_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-p`` is an instruction, not a guess: it keeps its reach.

    This is the other half of the rule. Narrowing the *guessed* proxy must not
    quietly narrow a proxy the user asked for, so the same unreachable address
    is fatal here and harmless above.
    """
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    destination = tmp_path / "out"
    args = app._default_get_args()
    args.targets = [file_server.url("file")]
    args.dir = str(destination)
    args.reporter = "quiet"
    args.retries = 1
    args.proxy = UNREACHABLE
    # Bounded so an unreachable proxy fails in seconds instead of waiting out the
    # socket timeout twice. Without the proxy this download finishes instantly,
    # which is what the test above shows.
    args.max_time = 3

    with pytest.raises(Exception, match="max-time|probe failed"):
        await app._get(args)
    assert not (destination / "file").exists()


async def test_the_environment_cannot_put_back_a_proxy_the_policy_exempted(
    file_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aiohttp re-reads ``http_proxy`` whenever ``proxy`` is ``None``.

    That undid the exclusion the release is about: with the environment proxy
    still exported, a loopback target the policy had answered "connect directly"
    for was handed to the proxy anyway. ``_build_config`` already folds the
    environment into the policy, so the session must not read it a second time.
    """
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, UNREACHABLE)
    # The WSL guess rewrites the host proxy to the other side of the boundary;
    # the policy then exempts this machine's targets, and the environment must
    # not be able to hand them back to the unreachable address.
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda _proxy: "http://10.255.255.254:7897")

    destination = tmp_path / "out"
    args = app._default_get_args()
    args.targets = [file_server.url("file")]
    args.dir = str(destination)
    args.reporter = "quiet"
    args.retries = 1

    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


def test_the_proxy_source_is_recorded_where_it_came_from(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy needs to know *why* a proxy is set, so the load records it."""
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    args = app._default_get_args()
    args.dir = str(tmp_path)
    assert app._build_config(args).proxy_source == ""

    args.proxy = "http://10.0.0.2:3128"
    explicit = app._build_config(args)
    assert explicit.proxy_source == "explicit"
    assert app._proxy_policy(explicit).scope == PROXY_ALL

    args.proxy = None
    monkeypatch.setenv("https_proxy", "http://10.0.0.3:3128")
    from_env = app._build_config(args)
    assert from_env.proxy_source == "env"
    assert app._proxy_policy(from_env).scope == PROXY_ALL


def test_a_guessed_proxy_is_recorded_as_a_guess(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: UNREACHABLE)

    args = app._default_get_args()
    args.dir = str(tmp_path)
    guessed = app._build_config(args)

    assert guessed.proxy_source == "wsl-guess"
    assert app._proxy_policy(guessed).scope == PROXY_EXTERNAL


def test_a_proxy_from_the_config_file_counts_as_explicit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value the user wrote down is not a guess, even though it is not ``-p``."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[core]\nproxy = "http://10.0.0.4:3128"\n', encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    args = app._default_get_args()
    args.dir = str(tmp_path)
    cfg = app._build_config(args)

    assert load_config().core.proxy == "http://10.0.0.4:3128"
    assert cfg.proxy_source == "explicit"
    assert cfg.core.proxy == "http://10.0.0.4:3128"


def test_a_guessed_proxy_is_never_written_back_to_the_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is a run-time fact; the config file cannot carry it."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[core]\ndir = '/tmp'\n", encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))

    cfg = Config()
    assert not hasattr(cfg.core, "proxy_source")
    assert cfg.proxy_source == ""
    policy = ProxyPolicy("", PROXY_ALL)
    assert policy.proxy_for("https://example.com/x") is None
