"""Every behaviour claim in the docs must name the test that proves it.

0.1.4's acceptance criteria include "each documented behaviour corresponds to a
test name". That is only checkable mechanically, so the documents cite real test
ids (``tests/integration/test_content_truth.py::test_...``) and this test fails
when a cited test is renamed, moved or deleted.

It also pins the *reverse* direction for the release's headline claims: the
primary documents must cite the content-truth and crash-safety suites, otherwise
"绝不静默损坏" would again be an unbacked promise.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "README.en.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
#: ``tests/x/y.py`` or ``tests/x/y.py::test_name`` (a ``[param]`` suffix is
#: allowed and ignored).
REFERENCE = re.compile(r"(tests/[\w./-]+\.py)(?:::(?P<name>\w+))?")
#: Documents that make the content-truth promise must cite its suite.
REQUIRED_CITATIONS = {
    "README.md": "tests/integration/test_content_truth.py",
    "README.en.md": "tests/integration/test_content_truth.py",
    "docs/design.md": "tests/integration/test_content_truth.py",
    "docs/design.zh.md": "tests/integration/test_content_truth.py",
}


def _documents() -> list[Path]:
    return [path for path in DOCUMENTS if path.is_file()]


def _references() -> list[tuple[Path, str, str | None]]:
    found: list[tuple[Path, str, str | None]] = []
    for path in _documents():
        text = path.read_text(encoding="utf-8")
        for match in REFERENCE.finditer(text):
            found.append((path, match.group(1), match.group("name")))
    return found


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def test_the_docs_cite_some_tests() -> None:
    assert len(_references()) >= 5, "the docs should back their claims with test ids"


def test_every_referenced_test_file_exists() -> None:
    missing = sorted(
        {relative for _doc, relative, _name in _references() if not (ROOT / relative).is_file()}
    )
    assert not missing, f"documented test files do not exist: {missing}"


def test_every_referenced_test_name_exists() -> None:
    """A renamed test must not leave the docs pointing at nothing."""
    cache: dict[str, set[str]] = {}
    broken: list[str] = []
    for doc, relative, name in _references():
        if name is None:
            continue
        if relative not in cache:
            path = ROOT / relative
            cache[relative] = _defined_tests(path) if path.is_file() else set()
        if name not in cache[relative]:
            broken.append(f"{doc.relative_to(ROOT)} -> {relative}::{name}")
    assert not broken, "documented tests no longer exist: " + "; ".join(broken)


@pytest.mark.parametrize(("document", "citation"), sorted(REQUIRED_CITATIONS.items()))
def test_the_headline_claims_cite_their_suite(document: str, citation: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    assert citation in text, f"{document} must cite {citation}"


def test_the_changelog_has_an_unreleased_section() -> None:
    """``RELEASING.md`` requires one; its absence hid unreleased work."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    assert "## [0.1.4]" in text


def test_the_documented_version_matches_the_package() -> None:
    from downctl import __version__

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in changelog
