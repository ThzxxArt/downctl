"""Scaffolding for new plugin packages (``downctl plugin new``).

The generated package is a *working* skeleton: it imports, registers, and its
own test passes before a single line is edited. A scaffold that does not run is
worse than none — it teaches the author that a red test is normal.
"""

from __future__ import annotations

import re
from pathlib import Path

from downctl.core.errors import DownctlError

#: Kinds that can be scaffolded, mapped to their entry-point group.
KINDS: dict[str, str] = {
    "source": "downctl.sources",
    "transport": "downctl.transports",
    "verifier": "downctl.verifiers",
    "reporter": "downctl.reporters",
    "router": "downctl.routers",
    "hook": "downctl.hooks",
    "jobsource": "downctl.jobsources",
    "auth": "downctl.auths",
    "server": "downctl.servers",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class _Kind:
    """The code one plugin kind needs."""

    def __init__(
        self,
        imports: str,
        base: str,
        members: str,
        *,
        extra_class_attrs: str = "",
        base_class: str = "",
    ) -> None:
        self.imports = imports
        self.base = base
        self.members = members
        self.extra_class_attrs = extra_class_attrs
        self.base_class = base_class


_KINDS: dict[str, _Kind] = {
    "source": _Kind(
        "from downctl.core.task import DownloadTask\nfrom downctl.plugin.protocols import API_VERSION",
        "Source",
        '''    def can_handle(self, target: str) -> bool:
        """Whether this plugin understands ``target``."""
        return False

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        """Expand ``target`` into one or more tasks."""
        raise NotImplementedError("resolve() is not implemented yet")
''',
    ),
    "transport": _Kind(
        "from contextlib import asynccontextmanager\nfrom collections.abc import AsyncIterator\n"
        "from downctl.plugin.protocols import API_VERSION, Response",
        "Transport",
        '''    sends_content_range = False

    def supports_range(self) -> bool:
        """Whether ranged (resumable) requests are supported."""
        return False

    @asynccontextmanager
    async def request(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[Response]:
        """Open ``url`` (optionally a byte range) and yield a response."""
        raise NotImplementedError("request() is not implemented yet")
        yield  # pragma: no cover - keeps the generator signature honest
''',
    ),
    "verifier": _Kind(
        "from downctl.plugin.protocols import API_VERSION",
        "Verifier",
        '''    async def verify(self, path: str, expected_hex: str) -> bool:
        """Return ``True`` when ``path`` matches ``expected_hex``."""
        raise NotImplementedError("verify() is not implemented yet")
''',
        extra_class_attrs='    algo = "{name}"\n',
    ),
    "reporter": _Kind(
        "from downctl.core.task import DownloadTask\nfrom downctl.plugin.protocols import API_VERSION",
        "Reporter",
        '''    def task_started(self, task: DownloadTask) -> None:
        """A file started."""

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        """``delta`` more bytes were transferred."""

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        """A file finished (``ok`` says whether it succeeded)."""
''',
    ),
    "router": _Kind(
        "from downctl.plugin.protocols import API_VERSION",
        "Router",
        '''    def rewrite(self, url: str) -> str:
        """Return the URL to actually fetch for ``url``."""
        return url
''',
    ),
    "hook": _Kind(
        "from collections.abc import Mapping\nfrom typing import ClassVar\n"
        "from downctl.plugin.hooks import ConfigurableHook\n"
        "from downctl.plugin.protocols import API_VERSION",
        "Hook",
        '''    OPTIONS: ClassVar[Mapping[str, str]] = {}

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        """Handle one lifecycle event (see downctl.plugin.hooks.EVENTS)."""
''',
        extra_class_attrs="",
        base_class="ConfigurableHook",
    ),
    "jobsource": _Kind(
        "from downctl.core.job import Job\nfrom downctl.plugin.protocols import API_VERSION",
        "JobSource",
        '''    formats = ("{name}",)

    def can_handle(self, path: str) -> bool:
        """Whether this plugin understands ``path``."""
        return path.lower().endswith("." + self.formats[0])

    async def load(self, path: str, ctx: RuntimeContext) -> list[Job]:
        """Parse ``path`` into jobs."""
        raise NotImplementedError("load() is not implemented yet")
''',
    ),
    "auth": _Kind(
        "from collections.abc import Mapping\n"
        "from downctl.core.credentials import AuthMaterial\n"
        "from downctl.plugin.protocols import API_VERSION",
        "Auth",
        '''    def can_handle(self, host: str) -> bool:
        """Whether this provider can produce material for ``host``."""
        return True

    async def material(
        self, host: str, ctx: RuntimeContext, options: Mapping[str, object]
    ) -> AuthMaterial | None:
        """Return the material for ``host``, or ``None``."""
        raise NotImplementedError("material() is not implemented yet")
''',
    ),
    "server": _Kind(
        "from collections.abc import Mapping\nfrom downctl.plugin.protocols import API_VERSION",
        "Server",
        '''    async def serve(
        self, root: str, ctx: RuntimeContext, options: Mapping[str, object]
    ) -> None:
        """Serve ``root`` until interrupted."""
        raise NotImplementedError("serve() is not implemented yet")
''',
    ),
}

_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{dist}"
version = "0.1.0"
description = "A downctl {kind} plugin"
readme = "README.md"
requires-python = ">=3.11"
dependencies = ["downctl>=0.1.6"]

[project.entry-points."{group}"]
{name} = "{package}.plugin:{cls}"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]
"""

_PLUGIN = '''\
"""{name}: a downctl {kind} plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

{imports}

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


class {cls}{bases}:
    """TODO: describe what this {kind} plugin does."""

    name = "{name}"
    api_version = API_VERSION
    priority = 0
{extra_attrs}
{members}
'''

_TEST = '''\
"""Tests for the {name} plugin."""

from __future__ import annotations

from downctl.plugin.registry import Registry
from downctl.plugin.testing import assert_registers, check_plugin

from {package}.plugin import {cls}


def test_contract() -> None:
    plugin = {cls}()
    assert check_plugin(plugin, "{kind}") == []
    assert_registers(Registry(), "{kind}", plugin)


def test_metadata() -> None:
    plugin = {cls}()
    assert plugin.name == "{name}"
'''

_README = """\
# {dist}

A [downctl](https://github.com/ThzxxArt/downctl) {kind} plugin.

## Install

```bash
pip install -e .
```

## Use

```bash
downctl plugin check {package}.plugin
downctl plugin list
```

## Test

```bash
python -m pytest
```
"""


def scaffold(name: str, kind: str, directory: str) -> list[str]:
    """Create a plugin package under ``directory``. Returns the files written."""
    if kind not in KINDS:
        raise DownctlError(f"unknown plugin kind {kind!r}; known: {', '.join(sorted(KINDS))}")
    if not _NAME_RE.match(name):
        raise DownctlError(
            f"plugin name {name!r} must be lower-case letters, digits and '_', "
            "starting with a letter"
        )
    package = f"{name}_plugin"
    root = Path(directory).expanduser().resolve() / package
    if root.exists() and any(root.iterdir()):
        raise DownctlError(f"refusing to scaffold into a non-empty directory: {root}")
    cls = "".join(part.capitalize() for part in name.split("_")) + _KINDS[kind].base
    spec = _KINDS[kind]
    package_dir = root / package
    tests_dir = root / "tests"
    package_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text(
        f'"""{package}: downctl plugin package."""\n', encoding="utf-8"
    )
    members = spec.members.replace("{name}", name)
    (package_dir / "plugin.py").write_text(
        _PLUGIN.format(
            name=name,
            kind=kind,
            imports=spec.imports,
            cls=cls,
            bases=f"({spec.base_class})" if spec.base_class else "",
            extra_attrs=spec.extra_class_attrs.replace("{name}", name),
            members=members,
        ),
        encoding="utf-8",
    )
    (tests_dir / "test_plugin.py").write_text(
        _TEST.format(name=name, kind=kind, package=package, cls=cls), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        _PYPROJECT.format(
            dist=f"{name}-plugin",
            kind=kind,
            group=KINDS[kind],
            name=name,
            package=package,
            cls=cls,
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        _README.format(dist=f"{name}-plugin", kind=kind, package=package),
        encoding="utf-8",
    )
    return [
        str(package_dir / "__init__.py"),
        str(package_dir / "plugin.py"),
        str(tests_dir / "test_plugin.py"),
        str(root / "pyproject.toml"),
        str(root / "README.md"),
    ]
