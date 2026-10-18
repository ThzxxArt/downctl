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
   ```

   The three per-module gates exist because a healthy global percentage can
   still hide the state machine that decides whether a file counts as finished
   (0.1.3 measured 87.86% overall while `core/engine.py` sat at 81%).

4. Commit and tag:

   ```bash
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin master --tags
   ```

5. The `Release` workflow verifies that the tag matches
   `src/downctl/__init__.py`, runs `twine check`, builds the sdist + wheel,
   publishes to PyPI, and creates a GitHub Release.

The tag/version check fails the release early instead of re-publishing an
older version to PyPI.

## Dry run locally

```bash
python -m build
twine check dist/*
```

## Versioning

[Semantic Versioning](https://semver.org/). While the plugin API is `1`
(`downctl.API_VERSION`), breaking changes to a plugin contract require an API
version bump and a migration note in the changelog.
