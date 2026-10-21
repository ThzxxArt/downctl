"""Built-in auth providers.

These are the ``[auth."<host>"] provider = ...`` implementations that ship with
downctl: an environment variable, a local command, and ``~/.netrc``. They use
the same plugin protocol as a third-party provider, so any of them can be
replaced by name.
"""

from __future__ import annotations

from downctl.builtin.auths.command_auth import CommandAuth
from downctl.builtin.auths.env_auth import EnvAuth
from downctl.builtin.auths.netrc_auth import NetrcAuth

__all__ = ["CommandAuth", "EnvAuth", "NetrcAuth"]
