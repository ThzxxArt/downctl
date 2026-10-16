"""Application orchestration for downctl.

Each ``cmd_*`` function maps to a CLI subcommand and receives the parsed
``argparse.Namespace``. They return a process exit code.

NOTE: This is the P1 skeleton. The engine wiring (plugin loading -> task
building -> download loop) lands in later phases; the handlers are explicit
placeholders so the CLI surface is testable end to end.
"""

from __future__ import annotations

import argparse

_NOT_IMPLEMENTED = "not implemented yet (P1 skeleton)"


def cmd_get(args: argparse.Namespace) -> int:
    print(f"downctl get: {_NOT_IMPLEMENTED}")
    print(f"  targets: {', '.join(args.targets)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    print(f"downctl probe: {_NOT_IMPLEMENTED}")
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    action = getattr(args, "plugin_action", None)
    if action == "info":
        print(f"downctl plugin info {args.name}: {_NOT_IMPLEMENTED}")
    else:
        print(f"downctl plugin list: {_NOT_IMPLEMENTED}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from downctl.config import config_path

    action = getattr(args, "config_action", None)
    if action == "path":
        print(config_path())
        return 0
    print(f"downctl config {action or 'show'}: {_NOT_IMPLEMENTED}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"downctl doctor: {_NOT_IMPLEMENTED}")
    return 0


def cmd_wizard(args: argparse.Namespace) -> int:
    print(f"downctl wizard: {_NOT_IMPLEMENTED}")
    return 0
