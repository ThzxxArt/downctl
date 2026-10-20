"""A dated changelog entry must not deny its own release.

The 0.1.4 entry said its tag had never been published, and the sentence was not
cosmetic: it was the stated reason for rewriting other sentences in that section
("nothing here describes behaviour that ever shipped"). The tag had in fact gone
to PyPI, so the changelog contained a claim the repository could falsify and no
check ever looked at.

This module makes the invariant explicit and mechanical: a ``## [x.y.z] - date``
heading *is* the assertion that ``x.y.z`` was released. A version that never
shipped belongs under ``## [Unreleased]``, where statements about not having
shipped are accurate. Anything under a dated heading that denies that release is
therefore a self-contradiction, and the release notes are the one document a user
reads to decide whether a fix reached them.

The check is deliberately narrow: it recognises the forms of denial that read as
a statement of fact about the entry's own version. It is not a grammar checker,
and it does not try to decide whether an arbitrary English sentence is true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"

#: ``## [version]`` with an optional ``- YYYY-MM-DD`` release date.
SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
#: Forms that deny that a version shipped. The release-status words are bound to
#: a negator; a bare mention ("a pre-release review", "release-candidate") is not
#: a denial and must not trip the check.
DENIAL = re.compile(
    r"\b(?:never|not)\s+(?:been\s+|yet\s+)?(?:published|released|shipped)\b"
    r"|\bdid\s+not\s+(?:ship|publish|release)\b"
    r"|\bnothing\b[^.;]{0,80}\bshipped\b"
    r"|\bunpublished\b",
    re.IGNORECASE,
)
#: A sentence boundary; used only to quote the offending text back to the author.
BOUNDARY = re.compile(r"[.;!?](?:\s|$)")


@dataclass(frozen=True)
class Section:
    """One ``## [version]`` block, with the source line of every body line."""

    version: str
    date: str | None
    body: tuple[tuple[str, int], ...]

    @property
    def released(self) -> bool:
        return self.date is not None


def _sections(text: str) -> list[Section]:
    lines = text.splitlines()
    heads: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = SECTION.match(line)
        if match is not None:
            heads.append((index, match))
    sections: list[Section] = []
    for position, (index, match) in enumerate(heads):
        end = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
        body: list[tuple[str, int]] = []
        fenced = False
        for offset in range(index + 1, end):
            line = lines[offset]
            if line.startswith("```"):
                fenced = not fenced
                continue
            if not fenced:
                body.append((line, offset + 1))
        sections.append(
            Section(
                version=match.group("version"),
                date=match.group("date"),
                body=tuple(body),
            )
        )
    return sections


def _flatten(body: tuple[tuple[str, int], ...]) -> tuple[str, list[int]]:
    """Join a section's lines into one string, remembering each character's line.

    Wrapping a sentence across lines must not hide it: the 0.1.4 sentence broke
    between "never" and "published", so a per-line scan never saw it.
    """
    chunks: list[str] = []
    line_of: list[int] = []
    for text, lineno in body:
        chunks.append(text)
        line_of.extend([lineno] * len(text))
        chunks.append(" ")
        line_of.append(lineno)
    return "".join(chunks), line_of


def _sentence(flat: str, index: int) -> str:
    start = 0
    for boundary in BOUNDARY.finditer(flat, 0, index):
        start = boundary.end()
    following = BOUNDARY.search(flat, index)
    end = following.end() if following is not None else len(flat)
    return " ".join(flat[start:end].split())


def denials(text: str) -> list[tuple[str, int, str]]:
    """Return ``(version, line, sentence)`` for every denial under a dated head.

    One sentence can contain two denials; it is reported once, because the
    author has one sentence to fix.
    """
    found: list[tuple[str, int, str]] = []
    seen: set[tuple[str, str]] = set()
    for section in _sections(text):
        if not section.released:
            continue
        flat, line_of = _flatten(section.body)
        for match in DENIAL.finditer(flat):
            sentence = _sentence(flat, match.start())
            key = (section.version, sentence)
            if key in seen:
                continue
            seen.add(key)
            found.append((section.version, line_of[match.start()], sentence))
    return found


def test_the_changelog_does_not_deny_its_own_releases() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    found = denials(text)
    assert not found, "a dated entry denies its own release:\n" + "\n".join(
        f"  CHANGELOG.md:{line} [{version}] {sentence}" for version, line, sentence in found
    )


def test_the_check_catches_the_paragraph_that_shipped_in_0_1_4() -> None:
    """The gate must fail on the exact text this test was written to retire.

    A checker nobody has seen fail is indistinguishable from one that always
    passes, so the historical sentence is kept here as a fixture.
    """
    text = (
        "## [0.1.4] - 2026-10-18\n"
        "\n"
        "They are fixed below, each with a regression that fails when the guard\n"
        "is removed. The tag was never\n"
        "published, so nothing here describes behaviour that ever shipped; where the\n"
        "review contradicted a sentence already written for this section, the sentence is\n"
        "corrected rather than annotated.\n"
    )
    found = denials(text)
    assert found, "the check failed to notice a section that denies its own release"
    versions = {version for version, _line, _sentence in found}
    assert versions == {"0.1.4"}


def test_the_check_reaches_across_a_line_wrap() -> None:
    assert denials("## [9.9.9] - 2030-01-01\n\nIt was never\nreleased.\n")
    assert denials("## [9.9.9] - 2030-01-01\n\nThat version did not ship.\n")
    assert denials("## [9.9.9] - 2030-01-01\n\nIt stays unpublished.\n")


def test_the_check_ignores_the_unreleased_section() -> None:
    """``[Unreleased]`` is exactly where "has not shipped yet" is the truth."""
    text = "## [Unreleased]\n\n- Not released yet: the 0.2.0 work.\n"
    assert not denials(text)


def test_the_check_ignores_release_words_that_are_not_denials() -> None:
    text = (
        "## [9.9.9] - 2030-01-01\n"
        "\n"
        "- A pre-release review, and a release-candidate process.\n"
        "- Yanking is not a delete.\n"
    )
    assert not denials(text)


def test_every_version_section_carries_a_release_date() -> None:
    """``## [x.y.z]`` without a date would make "released" ambiguous."""
    text = CHANGELOG.read_text(encoding="utf-8")
    undated = [section.version for section in _sections(text) if not section.released]
    assert undated == ["Unreleased"], f"undated version sections: {undated}"
