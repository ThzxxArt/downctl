"""Command-line interface for downctl.

This module owns argument parsing and command dispatch. The heavy lifting is
delegated to :mod:`downctl.app`. Subcommands are intentionally thin so the
plugin system can be exercised without pulling in the whole engine.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence

from downctl import __version__, console

PROG = "downctl"


def _verbosity_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="suppress non-error output",
    )
    parent.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="extra diagnostics",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    common = _verbosity_parent()
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="downctl — a plugin-based universal command-line downloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    add_get_parser(sub, common)
    add_batch_parser(sub, common)
    add_serve_parser(sub, common)
    add_auth_parser(sub, common)
    add_probe_parser(sub, common)
    add_status_parser(sub, common)
    add_verify_parser(sub, common)
    add_clean_parser(sub, common)
    add_plugin_parser(sub, common)
    add_config_parser(sub, common)

    sub.add_parser("doctor", parents=[common], help="diagnose environment (proxy / WSL / deps)")
    sub.add_parser("wizard", parents=[common], help="interactive download wizard")
    sub.add_parser("version", parents=[common], help="print version and exit")
    comp = sub.add_parser("completions", parents=[common], help="print a shell completion script")
    comp.add_argument(
        "shell",
        nargs="?",
        default="bash",
        choices=["bash", "zsh", "fish"],
        help="target shell (default: bash)",
    )

    return parser


def _add_download_options(p: argparse.ArgumentParser) -> None:
    """Options that describe *how* one group of targets is fetched.

    Shared by ``get`` and ``batch``: a batch job may override any of these,
    and sharing the parser is what keeps the two spellings identical.
    """
    p.add_argument("-d", "--dir", metavar="DIR", help="destination directory")
    p.add_argument("-j", "--jobs", type=int, default=None, help="concurrent files")
    p.add_argument(
        "--connections", type=int, default=None, help="per-file connections (default 16)"
    )
    p.add_argument("--stall", type=int, default=None, help="stall watchdog seconds (default 30)")
    p.add_argument(
        "--checkpoint",
        type=int,
        default=None,
        metavar="SECONDS",
        help="durable resume snapshot interval (default 30; 0 = only on exit)",
    )
    p.add_argument(
        "--piece-size", metavar="SIZE", default=None, help="piece size, e.g. 8M / 4M / 1m"
    )
    p.add_argument(
        "--min-split-size",
        metavar="SIZE",
        default=None,
        help="files smaller than SIZE use a single connection",
    )
    p.add_argument(
        "-t", "--limit-rate", metavar="RATE", help="rate limit, e.g. 5M / 1m / 512k (0 = none)"
    )
    p.add_argument(
        "-r",
        "--retries",
        type=int,
        default=None,
        help="max retries per target (default 10; 0 = retry forever)",
    )
    p.add_argument(
        "--max-time",
        type=int,
        default=None,
        metavar="SECONDS",
        help="give up on a single target after SECONDS (0 = no limit)",
    )
    p.add_argument(
        "--probe-jobs",
        type=int,
        default=None,
        metavar="N",
        help="how many targets are resolved/probed at once (default 8; 0 = follow --jobs)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-download from scratch, ignoring an existing file / resume state",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip files whose completion is provable (marker / bitmap / checksum)",
    )
    p.add_argument(
        "--require-witness",
        action="store_true",
        help="refuse to skip or resume state whose object version cannot be checked",
    )
    p.add_argument(
        "--recheck",
        action="store_true",
        help="re-hash existing files before skipping; implies --require-witness",
    )
    p.add_argument("--checksum", metavar="ALGO:HEX", help="verify checksum, e.g. sha256:...")
    p.add_argument("--checksums", metavar="FILE", help="verify against a sha256sum-style file")
    p.add_argument("-p", "--proxy", metavar="URL", help="proxy URL")
    p.add_argument(
        "--no-proxy",
        action="store_true",
        help="connect directly, ignoring a configured or environment proxy",
    )
    p.add_argument(
        "-H", "--header", action="append", default=[], metavar="K:V", help="extra request header"
    )
    p.add_argument("-u", "--user", metavar="USER:PASS", help="HTTP Basic auth credentials")
    p.add_argument("--mirror", metavar="FROM=TO", help="rewrite URL prefix")
    p.add_argument("--source", metavar="NAME", help="force a source plugin")
    p.add_argument("--reporter", metavar="NAME", help="progress reporter plugin")
    p.add_argument("--include", action="append", default=[], metavar="PAT")
    p.add_argument("--exclude", action="append", default=[], metavar="PAT")
    p.add_argument("--revision", metavar="REV", help="repo revision (default: main)")
    p.add_argument(
        "--hook",
        action="append",
        default=[],
        metavar="NAME",
        help="enable a hook plugin for this run",
    )
    p.add_argument("--user-agent", metavar="UA", help="custom HTTP User-Agent")
    p.add_argument(
        "--local-plugins",
        action="store_true",
        help="also load plugins from local plugin directories (untrusted)",
    )


def add_get_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("get", parents=[common], help="download one or more targets")
    p.add_argument("targets", nargs="*", metavar="<target>")
    _add_download_options(p)
    p.add_argument("-o", "--output", metavar="PATH", help="save single file to PATH")
    p.add_argument("--dry-run", action="store_true", help="probe only, do not download")
    p.add_argument("--retry-failed", action="store_true", help="retry last failed targets")
    p.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="keep going when a target cannot be resolved or probed",
    )
    p.add_argument("-l", "--list", metavar="FILE", help="read targets from a file")


def add_batch_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser(
        "batch",
        parents=[common],
        help="run the jobs described by a job file (TOML or JSON)",
    )
    p.add_argument("file", metavar="<file>")
    _add_download_options(p)
    p.add_argument(
        "--job",
        action="append",
        default=[],
        metavar="NAME",
        help="run only the named job (repeatable)",
    )
    p.add_argument("--job-source", metavar="NAME", help="force a job-source plugin")
    p.add_argument("--dry-run", action="store_true", help="probe every job, download nothing")
    p.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="keep running later jobs after one fails",
    )


def add_serve_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser(
        "serve",
        parents=[common],
        help="serve a directory so another machine can fetch it (resumable)",
    )
    p.add_argument("path", nargs="?", default=None, metavar="<dir|file>")
    p.add_argument(
        "--bind", default=None, metavar="ADDR", help="listen address (default 127.0.0.1)"
    )
    p.add_argument("--port", type=int, default=None, help="listen port (default 8080)")
    p.add_argument("--auth", metavar="USER:PASS", help="require HTTP Basic credentials")
    p.add_argument("--index", action="store_true", help="list directories")
    p.add_argument(
        "--quiet-logs",
        dest="serve_quiet",
        action="store_true",
        help="do not log requests",
    )


def add_auth_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("auth", parents=[common], help="inspect credential resolution")
    actions = p.add_subparsers(dest="auth_action", metavar="<action>")
    check = actions.add_parser(
        "check", parents=[common], help="show which provider a URL's host would use"
    )
    check.add_argument("urls", nargs="+", metavar="<url>")
    check.add_argument("--json", action="store_true", help="emit one JSON object per URL")


def add_probe_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("probe", parents=[common], help="probe size/status without downloading")
    p.add_argument("targets", nargs="+", metavar="<target>")
    p.add_argument("-p", "--proxy", metavar="URL", help="proxy URL")
    p.add_argument("--no-proxy", action="store_true", help="connect directly")
    p.add_argument("--json", action="store_true", help="emit one JSON object per target")


def add_status_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("status", parents=[common], help="show incomplete/failed downloads")
    p.add_argument("path", nargs="?", default=None, metavar="<dir>")
    p.add_argument("--json", action="store_true", help="emit a single JSON object")


def add_clean_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("clean", parents=[common], help="remove leftover bitmaps/failed log/locks")
    p.add_argument("path", nargs="?", default=None, metavar="<dir>")
    p.add_argument("--dry-run", action="store_true", help="list what would be removed")
    p.add_argument("--json", action="store_true", help="emit a single JSON object")


def add_verify_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser(
        "verify",
        parents=[common],
        help="prove completed files still match their source (exit 1 = mismatch, 3 = unprovable)",
    )
    p.add_argument("path", nargs="?", default=None, metavar="<dir>")
    p.add_argument("--json", action="store_true", help="emit a single JSON object")
    p.add_argument(
        "--checksums",
        metavar="FILE",
        help="prove content against a sha256sum-style manifest",
    )
    p.add_argument(
        "--repair",
        action="store_true",
        help="delete files proven bad, together with their markers (destructive)",
    )
    _add_local_plugins(p)


def _add_local_plugins(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--local-plugins",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also load plugins from local plugin directories (untrusted)",
    )


def add_plugin_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("plugin", parents=[common], help="manage plugins")
    _add_local_plugins(p)
    actions = p.add_subparsers(dest="plugin_action", metavar="<action>")
    listed = actions.add_parser("list", parents=[common], help="list discovered plugins")
    _add_local_plugins(listed)
    info = actions.add_parser("info", parents=[common], help="show details for a plugin")
    info.add_argument("name", metavar="<name>")
    _add_local_plugins(info)
    check = actions.add_parser(
        "check",
        parents=[common],
        help="load a plugin module/distribution and run the conformance checks",
    )
    check.add_argument("target", metavar="<module-or-dist>")
    _add_local_plugins(check)
    new = actions.add_parser("new", parents=[common], help="scaffold a plugin package")
    new.add_argument("name", metavar="<name>")
    new.add_argument(
        "--kind",
        default="source",
        choices=[
            "source",
            "transport",
            "verifier",
            "reporter",
            "router",
            "hook",
            "jobsource",
            "auth",
            "server",
        ],
        help="plugin category (default: source)",
    )
    new.add_argument("--dir", default=".", metavar="DIR", help="where to create the package")


def add_config_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    common: argparse.ArgumentParser,
) -> None:
    p = sub.add_parser("config", parents=[common], help="manage configuration")
    actions = p.add_subparsers(dest="config_action", metavar="<action>")
    init = actions.add_parser("init", parents=[common], help="write a default config file")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    actions.add_parser("show", parents=[common], help="print the effective config")
    actions.add_parser("path", parents=[common], help="print the config file path")


def main(argv: Sequence[str] | None = None) -> int:
    from downctl.platform_utils import init_console, safe_io

    init_console()  # Windows VT colors
    safe_io()  # UTF-8 output that survives redirection

    parser = build_parser()

    # Shell completion (optional): enable `register-python-argcomplete downctl`.
    with contextlib.suppress(ImportError):
        import argcomplete

        argcomplete.autocomplete(parser)

    args = parser.parse_args(argv)

    if getattr(args, "quiet", False):
        console.set_level(console.QUIET)
    elif getattr(args, "verbose", False):
        console.set_level(console.VERBOSE)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"{PROG} {__version__}")
        return 0

    from downctl import app

    dispatch = {
        "get": app.cmd_get,
        "batch": app.cmd_batch,
        "serve": app.cmd_serve,
        "auth": app.cmd_auth,
        "probe": app.cmd_probe,
        "status": app.cmd_status,
        "verify": app.cmd_verify,
        "clean": app.cmd_clean,
        "plugin": app.cmd_plugin,
        "config": app.cmd_config,
        "doctor": app.cmd_doctor,
        "wizard": app.cmd_wizard,
        "completions": app.cmd_completions,
    }
    handler = dispatch.get(args.command)
    if handler is None:  # pragma: no cover - argparse guarantees a known command
        parser.error(f"unknown command: {args.command}")
    # ``get``, ``batch`` and ``probe`` already guard themselves; wrapping the
    # dispatch keeps every other command's config/IO error a clean message +
    # exit code (and handles a closed stdout) instead of a traceback.
    return app._guard(lambda: int(handler(args)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
