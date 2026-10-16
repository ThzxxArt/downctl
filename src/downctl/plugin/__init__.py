"""downctl plugin framework.

See :mod:`downctl.plugin.protocols` for the contracts and
:mod:`downctl.plugin.loader` for discovery.
"""

from __future__ import annotations

from downctl.plugin.hooks import EVENTS, EventBus
from downctl.plugin.loader import discover
from downctl.plugin.protocols import (
    API_VERSION,
    Hook,
    Plugin,
    Reporter,
    Router,
    Sink,
    Source,
    Transport,
    Verifier,
)
from downctl.plugin.registry import CATEGORIES, PluginError, Registry

__all__ = [
    "API_VERSION",
    "CATEGORIES",
    "EVENTS",
    "EventBus",
    "Hook",
    "Plugin",
    "PluginError",
    "Registry",
    "Reporter",
    "Router",
    "Sink",
    "Source",
    "Transport",
    "Verifier",
    "discover",
]
