"""The release pipeline must not be able to publish an unverified commit.

0.1.4 shipped from a workflow whose ``publish`` job needed only ``build``: it had
no relationship with CI at all, so a red build, an untested commit or a missing
changelog entry could still become a PyPI release. These tests pin the shape of
the gate rather than its prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github/workflows/release.yml"

#: The workflows are deliberately not shipped in the sdist (they are release
#: machinery, not package content), so an installed tarball cannot run these
#: tests. Skipping must key on *that* fact, not on a workflow being missing:
#: inside a checkout, a deleted workflow still has to fail the suite.
pytestmark = pytest.mark.skipif(
    not (ROOT / ".git").exists(),
    reason="workflow files are not part of the sdist",
)


def _text() -> str:
    return RELEASE.read_text(encoding="utf-8")


def test_the_release_workflow_has_a_verify_job() -> None:
    assert re.search(r"^  verify:", _text(), re.MULTILINE), "release.yml lost its verify job"


def test_publishing_depends_on_the_verification() -> None:
    """``needs: [build]`` alone is what made the gate decorative."""
    assert "needs: [verify, build]" in _text()


def test_the_verification_is_a_precondition_of_the_build() -> None:
    assert re.search(r"^  build:\n(?:.*\n)*?    needs: \[verify\]$", _text(), re.MULTILINE)


def test_the_verification_checks_the_tag_history_ci_and_the_changelog() -> None:
    text = _text()
    # The commit behind the tag must be one that was merged to master...
    assert "is-ancestor" in text
    # ...that the CI workflow actually ran and passed on...
    assert "actions/workflows/ci.yml/runs" in text
    # ...and that the changelog already describes the version being released.
    assert "CHANGELOG.md has no" in text


def test_the_build_checks_the_installed_version_not_only_the_metadata() -> None:
    """``twine check`` reads metadata; this reads what the code will report."""
    assert '["downctl", "version"]' in _text()


# --------------------------------------------------------------------------- #
# quality gates that cannot silently stop gating
# --------------------------------------------------------------------------- #
def test_the_dev_extra_pins_a_coverage_that_supports_patch() -> None:
    """``patch = ["subprocess"]`` does nothing before coverage 7.6.

    The setting is accepted by older versions and ignored, so the documented
    "subprocess coverage" claim would be false with nothing failing.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"coverage>=([0-9.]+)"', text)
    assert match, "the dev extra must pin coverage explicitly"
    major, minor = (int(part) for part in match.group(1).split(".")[:2])
    assert (major, minor) >= (7, 6), f"coverage {match.group(1)} cannot apply `patch`"


def test_ci_asserts_the_patch_setting_is_live() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "patch: subprocess" in text


def test_no_workflow_step_can_be_configured_to_ignore_its_own_failure() -> None:
    """The gate above is made of text, so it must reject the mutation too.

    ``continue-on-error: true`` on the test step keeps every substring in place
    and turns the whole gate decorative: the job reports success while the
    tests it ran failed. Nothing in these workflows needs the key, so its
    presence is the signal.
    """
    offenders = [
        workflow.name
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml"))
        if "continue-on-error" in workflow.read_text(encoding="utf-8")
    ]
    assert not offenders, f"a step may not ignore its own failure: {offenders}"


# --------------------------------------------------------------------------- #
# the published artefact must be complete and readable where it lands
# --------------------------------------------------------------------------- #
def test_the_package_metadata_declares_that_it_is_typed() -> None:
    """``py.typed`` ships in the wheel; without the classifier it is ignored."""
    assert '"Typing :: Typed"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_documentation_url_points_at_the_docs_site() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'Documentation = "https://thzxxart.github.io/downctl/"' in text


def test_the_sdist_carries_what_it_needs_to_build_the_docs() -> None:
    """The tarball shipped ``docs/`` but not ``mkdocs.yml``.

    A source release that contains the pages and not the recipe cannot be built
    by the person who downloaded it.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.hatch.build.targets.sdist]")[1].split("\n[")[0]
    for name in ("mkdocs.yml", "CONTRIBUTING.md", "SECURITY.md", "RELEASING.md"):
        assert f'"/{name}"' in section, f"sdist omits {name}"


def test_ci_installs_the_sdist_and_builds_its_docs() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "dist/*.tar.gz" in text
    assert "mkdocs build --strict" in text


@pytest.mark.parametrize("readme", ["README.md", "README.en.md"])
def test_the_long_description_has_no_relative_links(readme: str) -> None:
    """PyPI resolves relative links against the project page, where they 404."""
    text = (ROOT / readme).read_text(encoding="utf-8")
    relative = sorted(set(re.findall(r"\]\((?!https?://|#|mailto:)([^)]+)\)", text)))
    assert not relative, f"{readme} has links that break on PyPI: {relative}"


# --------------------------------------------------------------------------- #
# supply chain and governance
# --------------------------------------------------------------------------- #
def test_every_action_is_pinned_to_a_commit() -> None:
    """A tag can be moved to point at different code; a SHA cannot."""
    unpinned: list[str] = []
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" not in line:
                continue
            target = line.split("uses:", 1)[1].strip()
            if target.startswith("./"):  # a local action
                continue
            _, _, ref = target.partition("@")
            ref = ref.split()[0]
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                unpinned.append(f"{workflow.name}: {target}")
    assert not unpinned, "actions must be pinned to a commit SHA: " + "; ".join(unpinned)


def test_dependabot_keeps_the_pins_current() -> None:
    """A pin nobody updates becomes a stale pin, which is its own risk."""
    path = ROOT / ".github/dependabot.yml"
    assert path.is_file(), "the pinned actions need Dependabot to move them"
    text = path.read_text(encoding="utf-8")
    assert "github-actions" in text
    assert "pip" in text


def test_the_security_policy_says_what_is_supported_and_where_to_report() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Supported versions" in text
    assert "/security/advisories/new" in text
    # A policy without a response target is a mailbox nobody checks.
    assert "working days" in text


def test_the_release_runbook_covers_yanking_and_candidates() -> None:
    text = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "twine yank" in text
    assert "Release candidates" in text
    # The workflow gates it describes must be the ones it actually has.
    assert "CI on that exact commit" in text


def test_the_0_1_3_advisory_is_published_in_the_docs() -> None:
    """An advisory nobody can find is not an advisory.

    It has to be reachable from the docs site (in ``nav``) and it has to say
    which versions are affected and how to check a file — the three things a
    reader arrives with.
    """
    advisory = ROOT / "docs/security-advisory-0.1.3-splicing.md"
    assert advisory.is_file()
    text = advisory.read_text(encoding="utf-8")
    assert "0.1.0 through 0.1.3" in text
    assert "downctl verify" in text
    assert "SHA256SUMS" in text
    assert "security-advisory-0.1.3-splicing.md" in (ROOT / "mkdocs.yml").read_text(
        encoding="utf-8"
    )
    # A Chinese reader gets the same page, not an English fallback.
    assert (ROOT / "docs/security-advisory-0.1.3-splicing.zh.md").is_file()
