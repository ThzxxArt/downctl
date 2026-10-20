"""Where a plugin's credential comes from.

Two rules, written once so no source can get them subtly different:

- an explicit ``[plugins.<name>]`` option always wins over the environment;
- the *source* is reported, because "why is this 401" is usually answered by
  "a different token is the one being used" — ``downctl doctor`` says which.

The environment variable names live here too: they are part of the same
contract, and a source that invented its own pair would be a fourth answer to
the same question.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: HuggingFace token variables, in priority order.
HF_TOKEN_ENVS: tuple[str, ...] = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
#: GitHub token variables, in priority order.
GITHUB_TOKEN_ENVS: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
#: AWS variables used when ``[plugins.s3]`` does not set a key.
AWS_ACCESS_KEY_ENVS: tuple[str, ...] = ("AWS_ACCESS_KEY_ID",)
AWS_SECRET_KEY_ENVS: tuple[str, ...] = ("AWS_SECRET_ACCESS_KEY",)
AWS_SESSION_TOKEN_ENVS: tuple[str, ...] = ("AWS_SESSION_TOKEN",)


@dataclass(frozen=True)
class Credential:
    """A resolved credential and where it came from."""

    value: str = ""
    #: ``"config"``, ``"env"`` or ``""`` when unset.
    source: str = ""
    #: The option key or environment variable that supplied it.
    origin: str = ""

    def __bool__(self) -> bool:
        return bool(self.value)


def resolve(options: Mapping[str, object], key: str, env_names: Sequence[str] = ()) -> Credential:
    """Resolve ``key`` from plugin options first, then the first set environment variable."""
    value = options.get(key)
    if isinstance(value, str) and value:
        return Credential(value, "config", key)
    for name in env_names:
        found = os.environ.get(name, "")
        if found:
            return Credential(found, "env", name)
    return Credential()
