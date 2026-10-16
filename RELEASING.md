# Releasing downctl

Releases are automated with GitHub Actions and PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no API
tokens stored).

## One-time setup

1. **Create the GitHub repository** `ThzxxArt/downctl` (public).
2. **PyPI**: register the project and add a trusted publisher
   - owner: `ThzxxArt`, repository: `downctl`
   - workflow: `release.yml`, environment: `pypi`
3. **TestPyPI**: same, environment `testpypi`.
4. **GitHub environments**: create `pypi` and `testpypi` (optionally require
   reviewers for `pypi`).

## Cutting a release

1. Update `CHANGELOG.md` (move `Unreleased` items under the new version).
2. Bump the version in `src/downctl/__init__.py` (`__version__`).
3. Commit and tag:

   ```bash
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```

4. The `Release` workflow builds the sdist + wheel, publishes to TestPyPI,
   then to PyPI, and creates a GitHub Release.

## Dry run locally

```bash
python -m build
twine check dist/*
```

## Versioning

[Semantic Versioning](https://semver.org/). While the plugin API is `1`
(`downctl.API_VERSION`), breaking changes to a plugin contract require an API
version bump and a migration note in the changelog.
