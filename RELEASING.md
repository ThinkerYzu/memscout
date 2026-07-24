# Releasing memscout

Releases are driven by GitHub Actions (`.github/workflows/`):

- **CI** (`ci.yml`) runs the test suite on every push to `master` and every pull request,
  across Python 3.9 / 3.11 / 3.13.
- **Release** (`release.yml`) fires when a `v*` tag is pushed: it verifies the tag matches the
  package version, runs the tests, builds the sdist + wheel, and creates a **GitHub Release**
  with those artifacts and auto-generated notes.

## Cutting a release

1. **Bump the version** in two places (they must match, and the CI release job enforces it):
   - `pyproject.toml` → `[project] version`
   - `memscout/__init__.py` → `__version__`

2. **Commit** the bump:

   ```bash
   git commit -am "Release v0.1.0"
   ```

3. **Tag and push** — the tag is `v` + the version:

   ```bash
   git tag v0.1.0
   git push origin master
   git push origin v0.1.0
   ```

4. The **Release** workflow runs and publishes the GitHub Release at
   `https://github.com/ThinkerYzu/memscout/releases`. Check the Actions tab if it doesn't appear.

## Versioning

Semantic-ish: bump the patch for fixes, the minor for new capabilities (a new decoder, a new
subcommand), the major for breaking API/CLI changes. Pre-1.0, minor bumps may include breaking
changes — note them in the release description.

## Optional: publish to PyPI

Not enabled yet. To add it, configure **Trusted Publishing** on PyPI for this repo (no API token
needed), then append a job to `release.yml`:

```yaml
  pypi:
    needs: release
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write        # OIDC for trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: python -m pip install --upgrade build && python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Until then, users install from source (`pip install git+https://github.com/ThinkerYzu/memscout`)
or from the GitHub Release artifacts.
