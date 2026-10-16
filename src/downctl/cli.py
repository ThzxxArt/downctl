"""Command-line interface for downctl.

This module owns argument parsing and command dispatch. The heavy lifting is
delegated to :mod:`downctl.app`. Subcommands are intentionally thin so the
plugin system can be exercised without pulling in the whole engine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from downctl import __version__

PROG = "downctl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="downctl — a plugin-based universal command-line downloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    add_get_parser(sub)
    add_probe_parser(sub)
    add_plugin_parser(sub)
    add_config_parser(sub)

    sub.add_parser("doctor", help="diagnose environment (proxy / WSL / deps / disk)")
    sub.add_parser("wizard", help="interactive download wizard")
    sub.add_parser("version", help="print version and exit")

    return parser


def add_get_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("get", help="download one or more targets")
    p.add_argument("targets", nargs="+", metavar="<target>")
    p.add_argument("-d", "--dir", metavar="DIR", help="destination directory")
    p.add_argument("-o", "--output", metavar="PATH", help="save single file to PATH")
    p.add_argument("-j", "--jobs", type=int, default=None, help="concurrent files")
    p.add_argument("-t", "--limit-rate", metavar="RATE", help="rate limit, e.g. 5M / 1m / 512k")
    p.add_argument("-r", "--retries", type=int, default=None, help="max retries (0 = infinite)")
    p.add_argument("--checksum", metavar="ALGO:HEX", help="verify checksum, e.g. sha256:...")
    p.add_argument("-p", "--proxy", metavar="URL", help="proxy URL")
    p.add_argument("--mirror", metavar="FROM=TO", help="rewrite URL prefix")
    p.add_argument("--source", metavar="NAME", help="force a source plugin")
    p.add_argument("--reporter", metavar="NAME", help="progress reporter plugin")
    p.add_argument("--include", action="append", default=[], metavar="PAT")
    p.add_argument("--exclude", action="append", default=[], metavar="PAT")
    p.add_argument("--dry-run", action="store_true", help="probe only, do not download")
    p.add_argument("--retry-failed", action="store_true", help="retry last failed targets")


def add_probe_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("probe", help="probe size/status without downloading")
    p.add_argument("targets", nargs="+", metavar="<target>")
    p.add_argument("-p", "--proxy", metavar="URL", help="proxy URL")


def add_plugin_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("plugin", help="manage plugins")
    actions = p.add_subparsers(dest="plugin_action", metavar="<action>")
    actions.add_parser("list", help="list discovered plugins")
    info = actions.add_parser("info", help="show details for a plugin")
    info.add_argument("name", metavar="<name>")


def add_config_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("config", help="manage configuration")
    actions = p.add_subparsers(dest="config_action", metavar="<action>")
    actions.add_parser("init", help="write a default config file")
    actions.add_parser("show", help="print the effective config")
    actions.add_parser("path", help="print the config file path")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"{PROG} {__version__}")
        return 0

    from downctl import app

    dispatch = {
        "get": app.cmd_get,
        "probe": app.cmd_probe,
        "plugin": app.cmd_plugin,
        "config": app.cmd_config,
        "doctor": app.cmd_doctor,
        "wizard": app.cmd_wizard,
    }
    handler = dispatch.get(args.command)
    if handler is None:  # pragma: no cover - argparse guarantees a known command
        parser.error(f"unknown command: {args.command}")
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
