# Release Guide: Local Build + PyPI Publish

Steps to build `pow-rag-mcp` locally and publish a new version through the
existing CI pipeline (`.github/workflows/release.yml`).

## 1. Files to update for a new version

Both files below must be bumped **to the exact same version string** before
tagging. The release workflow's `check-version` job compares the pushed tag
against `pyproject.toml`'s version with an exact string match (no
normalization) — a mismatch fails the release before anything is published.

| File | Field | Notes |
|---|---|---|
| `pyproject.toml` | `[project] version = "X.Y.Z"` | Source of truth checked by CI against the tag. |
| `src/rag_mcp/data/server_info.json` | `"version": "X.Y.Z"` | Reported by the MCP server at runtime (`server_info["version"]`). Not checked by CI, but should stay in sync so the running server reports the correct version. |

Version must be valid [SemVer 2.0.0](https://semver.org) (`MAJOR.MINOR.PATCH`,
optional `-prerelease`/`+build`), and must be strictly greater than the
version currently published on PyPI for `pow-rag-mcp` — CI checks this via
PyPI's JSON API automatically.

## 2. Local build (dist) — optional sanity check before pushing

Run from the repo root, using the project's `.venv`:

```powershell
.venv\Scripts\python.exe -m pip install build twine
.venv\Scripts\python.exe -m build
.venv\Scripts\python.exe -m twine check dist\*
```

This produces `dist/pow_rag_mcp-X.Y.Z-py3-none-any.whl` and
`dist/pow_rag_mcp-X.Y.Z.tar.gz`. `twine check` validates metadata/README
rendering the same way the CI `check-version` job does.

Optional: test-install the local wheel in an isolated venv before publishing:

```powershell
python -m venv .release-test-venv
.release-test-venv\Scripts\python.exe -m pip install dist\pow_rag_mcp-X.Y.Z-py3-none-any.whl
.release-test-venv\Scripts\rag-mcp.exe config
```

Clean up afterwards:

```powershell
Remove-Item -Recurse -Force .release-test-venv, dist, build, src\pow_rag_mcp.egg-info
```

## 3. Git commands to trigger the publish pipeline

CI publishes only on a pushed tag matching `v*` (see `on.push.tags` in
`release.yml`), so publishing is just: commit the version bump, then push a
matching tag.

```powershell
git add pyproject.toml src\rag_mcp\data\server_info.json
git commit -m "chore: bump version to X.Y.Z"
git push origin <branch>

git tag vX.Y.Z
git push origin vX.Y.Z
```

Replace `X.Y.Z` with the new version (e.g. `1.1.6`) and `<branch>` with the
branch you're releasing from (must be pushed/merged before tagging so the
tag points at the commit containing the version bump).

## 4. What happens after the tag is pushed

The `Release` workflow runs automatically: `test` → `build` → `check-version`
(tag vs `pyproject.toml` vs latest PyPI version) → `publish-testpypi` →
`verify-testpypi-install` → `publish-pypi` → `verify-pypi-install`. No further
manual steps are needed — watch the Actions tab for the run triggered by the
tag push.

To re-run without creating a new tag (e.g. after fixing a CI-only issue),
use `workflow_dispatch` from the Actions tab instead of pushing a new tag.

## 5. Picking up a new version via `uvx`

`uvx --from pow-rag-mcp rag-mcp ...` re-resolves against PyPI on each
invocation, but `uv` caches downloaded distributions — after a new release,
a stale cached build can still get used. Note the package/command name
split: the PyPI distribution is `pow-rag-mcp`, but the installed console
script is `rag-mcp`, so `--from` is required (`uvx pow-rag-mcp ...` alone
will fail with a "not found in the package registry" resolution error).

Force a fresh resolve/download, ignoring the cache:

```powershell
uvx --refresh --from pow-rag-mcp rag-mcp serve --no-reindex
```

Or scope the refresh to just this package instead of everything cached:

```powershell
uvx --refresh-package pow-rag-mcp --from pow-rag-mcp rag-mcp serve --no-reindex
```

To confirm a specific version is what actually gets resolved (useful right
after a release to verify it landed):

```powershell
uvx --from pow-rag-mcp==X.Y.Z rag-mcp config
```

To clear the cached distribution entirely for a clean slate:

```powershell
uv cache clean pow-rag-mcp
```

In most cases none of this is needed — `uvx` checks PyPI's index fresh each
time it resolves, so simply restarting the MCP connection (e.g. in Kiro's
`mcp.json`) after a new PyPI release picks up the latest version. The
`--refresh`/`--refresh-package` flags are only needed for edge cases, such
as verifying immediately after a Test PyPI + PyPI publish in quick
succession.
