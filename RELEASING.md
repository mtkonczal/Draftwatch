# Releasing draftwatch

Versions are git tags (`vX.Y.Z`). The publish workflow
(`.github/workflows/publish.yml`) builds and uploads to PyPI on tag push via
**trusted publishing** (no API tokens stored anywhere). It is inert until the
one-time PyPI setup below is done.

## One-time setup (before the first publish)

1. ~~Move draftwatch to its own repository~~ Done: `mtkonczal/Draftwatch`.
2. Create an account / sign in at <https://pypi.org>. Verify the email address
   and enable 2FA (PyPI requires it before you can manage publishing).
3. PyPI → *Your projects* → *Publishing* → **Add a new pending publisher**
   (under "GitHub"):
   - PyPI project name: `draftwatch` (confirmed free 2026-07-02 and 2026-07-04)
   - Owner: `mtkonczal`
   - Repository: `Draftwatch`
   - Workflow name: `publish.yml`
   - Environment: `pypi`
4. GitHub repo → *Settings* → *Environments* → create an environment named
   `pypi` (optionally add yourself as a required reviewer — that makes every
   publish a manual approval).

## Every release

1. Update `__version__` and `RELEASE_DATE` in `draftwatch/app.py` (the single
   source — `__init__.py` re-exports `__version__` and the About panel shows
   both), then set the matching `version` in `pyproject.toml`.
2. Add a dated section to `CHANGELOG.md`.
3. Make sure both suites are green:
   `python3 testing/test_reconstruct.py && python3 testing/test_acceptance.py`
   (and, after frontend changes, `node scripts/smoke_frontend.mjs`).
4. Commit, then tag and push:

   ```bash
   git tag v0.1.0
   git push origin main --tags
   ```

5. The `publish` workflow builds the sdist/wheel with `python -m build`,
   verifies them with `twine check`, and uploads via trusted publishing.

## Sanity check after publishing

```bash
uvx draftwatch@latest --help
```

## Local dry run (no upload)

```bash
python3 -m pip install build twine
python3 -m build
twine check dist/*
```
