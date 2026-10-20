# Releasing downctl

Releases are automated with GitHub Actions and PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no API
tokens stored).

## One-time setup

1. **Create the GitHub repository** `ThzxxArt/downctl` (public).
2. **PyPI**: register the project and add a trusted publisher
   - owner: `ThzxxArt`, repository: `downctl`
   - workflow: `release.yml`, environment: `pypi`
3. **GitHub environments**: create `pypi` (optionally require reviewers).

## Cutting a release

1. Update `CHANGELOG.md` (move `Unreleased` items under the new version).
2. Bump the version in `src/downctl/__init__.py` (`__version__`).
3. Run the full local gate (this is what CI enforces):

   ```bash
   ruff check . && ruff format --check . && mypy
   pytest --cov=downctl --cov-report=term-missing --cov-fail-under=85
   coverage report --include="*/downctl/core/pieces.py"   --fail-under=95
   coverage report --include="*/downctl/core/transfer.py" --fail-under=95
   coverage report --include="*/downctl/core/engine.py"   --fail-under=95
   mkdocs build --strict
   python -m build && twine check dist/*
   ```

   The three per-module gates exist because a healthy global percentage can
   still hide the state machine that decides whether a file counts as finished
   (0.1.3 measured 87.86% overall while `core/engine.py` sat at 81%).

4. Commit, push, and **wait for CI on that exact commit to go green**:

   ```bash
   git commit -am "release: vX.Y.Z"
   git push origin master
   ```

5. Tag the commit that CI passed on, and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "downctl vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. The `Release` workflow **verifies before it builds**:
   - the tag is on `master`'s history,
   - `src/downctl/__init__.py` says the same version,
   - `CHANGELOG.md` has a `## [X.Y.Z]` section,
   - the CI workflow ran on this commit and succeeded.

   Only then does it build, `twine check`, install the wheel to confirm the
   version it reports, publish to PyPI, and create a GitHub Release. A failed
   gate stops everything before anything is uploaded.

## After publishing

Do these before telling anyone the release exists:

1. **PyPI page** — `https://pypi.org/project/downctl/X.Y.Z/`. Check the version,
   the `Requires-Python`, and that the long description rendered (links in the
   README are absolute so they resolve from PyPI).
2. **Hashes match** — compare the wheel and sdist SHA-256 on PyPI with the
   GitHub Release assets. They are built once and uploaded once; a mismatch
   means the wrong artefact was published.
3. **Install from a clean environment**:

   ```bash
   python -m venv /tmp/check && /tmp/check/bin/pip install downctl==X.Y.Z
   /tmp/check/bin/downctl version
   ```

4. **Smoke the CLI** — `downctl get --help`, `downctl plugin list`.
5. **Tell the people affected** by anything that changed behaviour. If the
   release includes a fix for published data (as 0.1.5 does for the 0.1.0–0.1.3
   splicing defect), the advisory has to be published too — the release notes
   alone are not where a user with a corrupt file looks.

PyPI's JSON API and the project page can lag a minute behind the upload; a
fresh-index check that still shows the old version is cache, not a failed
publish. Confirm with an actual install.

## If a release is wrong

A published version cannot be replaced: the file name is taken forever, and a
re-upload is rejected. The options, in order of preference:

| Situation | Action |
|---|---|
| A packaging mistake (missing file, broken metadata) | Publish the next patch with the fix. Do **not** yank unless the artefact is unusable — yanking hides a working release from everyone who pins it |
| A defect that produces wrong output or loses data | Publish a patch with the fix **and** an advisory describing what to check. Yank only if installing the broken version is worse than not installing it |
| A security problem | Private fix, advisory, then the release. Coordinate the disclosure date in the advisory |
| The release was never installable | `yank` it, publish the next patch |

Yanking does not delete anything: it removes the version from resolution while
leaving existing pins working. Say so in the changelog entry of the replacement.

```bash
# yank (sets the flag; the files stay)
python -m twine yank -y downctl X.Y.Z
```

## Release candidates

For a change large enough that a broken release would be expensive, publish a
candidate first:

1. Set `__version__ = "X.Y.Zrc1"` and add `## [X.Y.Zrc1]` to the changelog. The
   tag is `vX.Y.Zrc1` — the workflow compares the two strings exactly.
2. Push the tag as usual. `pip install downctl==X.Y.Zrc1` works, but no plain
   `pip install downctl` picks it up, so nothing is disturbed.
3. If it is wrong, publish `X.Y.Zrc2`; a candidate is never overwritten either.
4. When it is right, bump `__version__` to `X.Y.Z`, add the final changelog
   entry, and release normally.

Mark the GitHub Release for a candidate as a pre-release so it does not become
the "latest" download.

## Versioning

[Semantic Versioning](https://semver.org/). While the plugin API is `1`
(`downctl.API_VERSION`), breaking changes to a plugin contract require an API
version bump and a migration note in the changelog.

The patch line is not a promise that nothing observable changed: a fix can
correct a default that was wrong (0.1.5 stops sending the HF token to a
configured mirror, and narrows a guessed proxy). What a patch release does
promise is that the change is described in `docs/upgrading.md` before it ships.
