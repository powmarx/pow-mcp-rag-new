# pip / uvx Install Guide (Phase 2)

Install `rag-mcp` without Docker. This guide covers four flows:

- **[Public PyPI install](#public-pypi-install)** — recommended for new users with no repo
  checkout: `pip install pow-rag-mcp`, `uvx --from pow-rag-mcp`, or `uv tool install
  pow-rag-mcp`.
- **[Local index install](#local-index-install)** — recommended for maintainers and
  contributors testing unpublished changes: build a wheel locally and serve it from a local (or
  later, hosted) package index.
- **[Local release dry run](#local-release-dry-run)** — build and validate a release locally
  before pushing a release tag.
- **[Post-publish verification](#post-publish-verification)** — manually re-check that a
  published release installs and runs correctly.

For the Docker alternative, see [DOCKER_GUIDE.md](DOCKER_GUIDE.md).

---

## Public PyPI install

Recommended for new users who don't have (and don't want) a local checkout of this repository.
None of the options below need a repo checkout or any local package index configuration
(`--extra-index-url`, `--index-url`, or a running `pypiserver`) — every command talks directly to
the public `pypi.org` index.

> **Distribution name:** the package is published as `pow-rag-mcp` — not `rag-mcp` or
> `rag-mcp-server`, which are unrelated packages already registered on PyPI under those names.
> Whichever install method you pick below, the command you run afterwards is always `rag-mcp`
> (the console script name is unchanged).

**Minimum Python version:** `3.11` (matches `requires-python` in `pyproject.toml`).

### Option 1: One-off execution with `uvx`

```bash
uvx --from pow-rag-mcp rag-mcp serve
```

Runs the MCP server directly without a persistent install. `uv` resolves and caches the
dependency tree in an isolated environment on first use. No repo checkout and no local package
index configuration needed — `uvx` resolves `pow-rag-mcp` directly from PyPI.

Other subcommands work the same way, e.g.:

```bash
uvx --from pow-rag-mcp rag-mcp config   # show resolved config and data paths
uvx --from pow-rag-mcp rag-mcp index    # index configured projects
```

### Option 2: Persistent install with `uv tool install`

```bash
uv tool install pow-rag-mcp
```

Installs a stable `rag-mcp` executable on your PATH (resolved once, not on every invocation). No
repo checkout and no local package index configuration needed. Upgrade later with:

```bash
uv tool upgrade pow-rag-mcp
```

### Option 3: Traditional `pip`

```bash
pip install pow-rag-mcp
```

Installs `rag-mcp` into whichever Python environment is active (a virtualenv is recommended). No
repo checkout and no local package index configuration needed. Upgrade later with:

```bash
pip install --upgrade pow-rag-mcp
```

### After installing

All three options expose the same `rag-mcp` console script:

```bash
rag-mcp config   # seeds config on first run, shows resolved paths
rag-mcp index    # index configured projects
rag-mcp serve    # start the MCP server (stdio)
```

Config seeding, `config.yaml` layout, and project setup are identical regardless of which
install method you used — see [First run — config seeding](#first-run--config-seeding) and
[Configure projects](#configure-projects) further down.

### MCP config

If you installed with `pip install pow-rag-mcp` or `uv tool install pow-rag-mcp` (both put
`rag-mcp` on PATH):

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "rag-mcp",
      "args": ["serve", "--no-reindex"],
      "env": {
        "HF_HUB_OFFLINE": "1"
      },
      "disabled": false,
      "autoApprove": [
        "search_docs", "search_specs", "search_code", "search_logs",
        "list_projects", "list_files", "get_document", "get_project_summary",
        "find_function", "find_variable", "search_hex_pattern", "compare_projects",
        "add_project", "add_file", "add_folder", "add_pattern", "index_log_file"
      ]
    }
  }
}
```

If you'd rather not install anything persistently, point `command` at `uvx` instead:

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "uvx",
      "args": ["--from", "pow-rag-mcp", "rag-mcp", "serve", "--no-reindex"],
      "env": {
        "HF_HUB_OFFLINE": "1"
      },
      "disabled": false,
      "autoApprove": [
        "search_docs", "search_specs", "search_code", "search_logs",
        "list_projects", "list_files", "get_document", "get_project_summary",
        "find_function", "find_variable", "search_hex_pattern", "compare_projects",
        "add_project", "add_file", "add_folder", "add_pattern", "index_log_file"
      ]
    }
  }
}
```

---

## Local index install

Recommended for maintainers and contributors testing changes that haven't been published to
PyPI yet — **not** for new users, who should use [Public PyPI install](#public-pypi-install)
above instead. This flow builds a wheel from your local checkout and serves it from a local (or
later, hosted) package index.

### Local PyPI + uvx mode

This runs a minimal PyPI-compatible index (`pypiserver`) on your machine, serving the
`pow-rag-mcp` wheel (built from this checkout's `pyproject.toml`) from a `packages/` folder in
this repo. `uv`/`uvx` install and run the package from that index in an isolated, cached
environment — no venv to manage, no Docker.

> **Windows note:** `setup-pypi.bat` defaults to `uv tool install` (a one-time resolve into a
> persistent exe at `~/.local/bin/rag-mcp.exe`) rather than `uvx --from` (which re-resolves
> the ~110-package dependency tree on every single MCP connection). The repeated resolution
> intermittently races Windows Defender's real-time scanner on `uv`'s trampoline `.exe` write,
> failing with `Failed to update Windows PE resources... Acesso negado`. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#uvx-install-fails-with-failed-to-update-windows-pe-resources-acesso-negado)
> for details. `uv tool install` avoids the problem entirely by only resolving once.

#### 1. One-command setup

```bash
cd <your-checkout>/pow-mcp-rag-new
setup-pypi.bat
```

This:
1. Creates a small build-only venv (`.venv/`) with `build` + `pypiserver` (not used at runtime)
2. Syncs `config/*.yaml|json` into `src/rag_mcp/data/` so they're bundled inside the wheel
3. Builds the wheel (`python -m build --wheel`) into `dist/`
4. Copies the wheel into `packages/` (the local index's package folder)
5. Installs the built package via `uv tool install`/`uvx` — resolves the dependency tree once
   and installs a stable exe at `~/.local/bin/rag-mcp.exe`
6. Writes/updates the `rag-mcp` entry in `~/.kiro/settings/mcp.json` (if `~/.kiro` exists)
   and `.vscode/mcp.json`, both pointing directly at that exe (falls back to `uvx --from` if
   `uv tool install` isn't available or fails)

#### 2. Start the local index

The setup script does **not** keep the index server running (it's a one-shot build). Start it
yourself and leave it running while Kiro / VS Code are connected:

```powershell
.venv\Scripts\python.exe -m pypiserver run -p 8080 packages --disable-fallback
```

Verify it's serving the package:

```powershell
Invoke-WebRequest -Uri "http://localhost:8080/simple/pow-rag-mcp/" -UseBasicParsing
```

> **Tip:** Run this as a background/startup task (Task Scheduler, or `control_pwsh_process`-style
> background terminal in Kiro) so it survives reboots without manual intervention.

#### 3. Restart Kiro / reconnect the MCP server

With the default `--stable` mode, the server starts instantly (no dependency resolution — it
already happened once during `setup-pypi.bat`). If `mcp.json` falls back to `uvx --from` (stable
install unavailable), the first connection downloads and caches all dependencies (~10-30s, ~110
packages including torch/chromadb); subsequent connections reuse `uv`'s cache and start in ~1-2s
— but every launch still re-resolves the tree, which is what triggers the intermittent Windows
Defender race described above.

#### 4. Index your projects

```bash
$env:RAG_CONFIG_PATH = "D:/GitHub/pow-mcp-rag-new/config/config.yaml"   # or use the seeded XDG path
uvx --extra-index-url http://localhost:8080/simple/ --from pow-rag-mcp rag-mcp index
```

Or ask Kiro chat to add/index projects once the MCP server is connected (see
[TOOLS_GUIDE.md](TOOLS_GUIDE.md) — `add_project`, `add_pattern`).

#### Rebuild and republish after code changes

Re-run `setup-pypi.bat` any time you change server code — it rebuilds the wheel, overwrites the
file in `packages/`, and re-runs `uv tool install --force` so the persistent exe picks up the new
build immediately (no manual reinstall step, no mcp.json changes needed since the exe path is
stable).

If managing the stable install manually instead of via the script:

```bash
uv tool install --extra-index-url http://localhost:8080/simple/ --force pow-rag-mcp
```

> Bump `version` in `pyproject.toml` when publishing a new build so cache invalidation is
> unambiguous — reusing the same version number with different content can serve stale cached
> wheels in some edge cases, for both `uv tool install` and `uvx --from`.


### Configuring a remote machine (no repo checkout)

`scripts/setup_mcp_config.py` (used by `setup-pypi.bat`) needs the full repo — it reads
`config/server_info.json` and defaults `.vscode/mcp.json` to the repo root. For a machine that
only needs to *consume* an already-published index (local or hosted) without cloning
`pow-mcp-rag-new` at all, use `scripts/remote_mcp_setup.py` instead — it has zero dependencies
beyond the Python standard library and can run as a single file:

```bash
# 1. Install the package as a persistent tool (resolves once, avoids the
#    per-launch Windows Defender trampoline-exe race — see TROUBLESHOOTING.md)
uv tool install --extra-index-url <INDEX_URL> pow-rag-mcp

# 2. Write mcp.json — no repo needed, just this one script
uv run remote_mcp_setup.py --index-url <INDEX_URL> --stable

# Or with plain Python (uv itself not required for this script):
python remote_mcp_setup.py --index-url <INDEX_URL> --stable
```

Since there's no repo root to default to, `.vscode/mcp.json` is only written if you pass
`--vscode-dir <path>` explicitly. Kiro's `~/.kiro/settings/mcp.json` is written by default
(use `--skip-kiro` to opt out). Run `python remote_mcp_setup.py --help` for the full option list
(custom server name, custom mcp.json path, omitting `--no-reindex`, etc.).

The script merges into any existing `mcp.json`/`.vscode/mcp.json` without touching other
configured servers, and backs up + recreates the file if it finds malformed JSON.

### Direct pip install (alternative to uvx)

#### Install from the local wheel

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install dist/pow_rag_mcp-1.0.0-py3-none-any.whl
```

#### Install from the local PyPI index

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install pow-rag-mcp --extra-index-url http://localhost:8080/simple/
```


### First run — config seeding

On first run the CLI automatically creates a config file from the bundled template (packaged
inside the wheel at `rag_mcp/data/config.template.yaml` — kept in sync with `config/` via
`scripts/sync_package_data.py`, which `setup-pypi.bat` runs automatically before building):

| Platform | Config path |
|----------|------------|
| Windows  | `%APPDATA%\rag-mcp\config.yaml` |
| Linux    | `~/.config/rag-mcp/config.yaml` |
| macOS    | `~/.config/rag-mcp/config.yaml` |

```bash
rag-mcp config       # show resolved config and data paths
```

> If the bundled template is somehow missing from the installed package, a minimal stub is
> created instead (matching the current defaults: `BAAI/bge-small-en-v1.5` embedding model,
> reranker enabled) and a warning is printed to stderr. This should not happen with wheels built
> via `setup-pypi.bat` / `scripts/sync_package_data.py`, but is a safety net for manually built
> packages that skip that step.

### Configure projects

Edit the seeded config file to add your projects (same `config.yaml` format as Docker):

```yaml
projects:
  - name: my-api
    description: My API docs
    base_path: /path/to/my-project
    sources:
      - pattern: "src/**/*.h"
        type: header
        description: Headers
```

Override paths via env vars if needed:
- `RAG_CONFIG_PATH` — explicit config file location (also used by Docker)
- `RAG_DATA_PATH` — ChromaDB storage directory

### Index and serve

```bash
# Index all projects
rag-mcp index

# Index one project
rag-mcp index --project my-api

# Start MCP server (stdio — for Kiro)
rag-mcp serve

# Start HTTP server (for Odysseus or other HTTP clients)
rag-mcp serve --http --port 8000

# Full reset + reindex
rag-mcp index --reset
```

See [CLI_REFERENCE.md](CLI_REFERENCE.md) (or `rag-mcp docs cli`) for every flag and
environment variable across all four subcommands (`serve`, `index`, `config`, `docs`).

### MCP config

`setup-pypi.bat` writes both of these automatically (in `--stable` mode by default — see the
Windows note above). Shown here for reference / manual setup.

#### Kiro (`~/.kiro/settings/mcp.json`) — stable tool exe (default, recommended on Windows)

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "C:/Users/you/.local/bin/rag-mcp.exe",
      "args": ["serve", "--no-reindex"],
      "env": {
        "HF_HUB_OFFLINE": "1"
      },
      "disabled": false,
      "autoApprove": [
        "search_docs", "search_specs", "search_code", "search_logs",
        "list_projects", "list_files", "get_document", "get_project_summary",
        "find_function", "find_variable", "search_hex_pattern", "compare_projects",
        "add_project", "add_file", "add_folder", "add_pattern", "index_log_file"
      ]
    }
  }
}
```

Install/update the exe with: `uv tool install --extra-index-url <index_url> --force pow-rag-mcp`

> **`HF_HUB_OFFLINE=1`:** Suppresses `sentence-transformers`'/`huggingface_hub`'s network
> reachability check on every startup ("Warning: You are sending unauthenticated requests to the
> HF Hub..."). Safe once the embedding model (`BAAI/bge-small-en-v1.5`) and reranker
> (`cross-encoder/ms-marco-MiniLM-L-6-v2`) are already cached locally (they are, after the first
> successful run) — `sentence-transformers` loads straight from `~/.cache/huggingface/hub/`
> without touching the network. If you later switch to a model that isn't cached yet, temporarily
> remove this env var (or delete it from `mcp.json`) so the first download can happen, then add
> it back.

#### Kiro — `uvx --from` (fallback, re-resolves on every launch)

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "uvx",
      "args": [
        "--extra-index-url", "http://localhost:8080/simple/",
        "--from", "pow-rag-mcp",
        "rag-mcp", "serve", "--no-reindex"
      ],
      "env": {
        "HF_HUB_OFFLINE": "1"
      },
      "disabled": false,
      "autoApprove": [
        "search_docs", "search_specs", "search_code", "search_logs",
        "list_projects", "list_files", "get_document", "get_project_summary",
        "find_function", "find_variable", "search_hex_pattern", "compare_projects",
        "add_project", "add_file", "add_folder", "add_pattern", "index_log_file"
      ]
    }
  }
}
```

#### VS Code / VS 2026 (`.vscode/mcp.json`) — stable tool exe

```json
{
  "servers": {
    "rag-mcp": {
      "type": "stdio",
      "command": "C:/Users/you/.local/bin/rag-mcp.exe",
      "args": ["serve", "--no-reindex"],
      "env": {
        "HF_HUB_OFFLINE": "1"
      }
    }
  }
}
```

Or with a plain `pip install`ed console script (no `uvx`):

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "rag-mcp",
      "args": ["serve", "--no-reindex"],
      "env": {
        "HF_HUB_OFFLINE": "1"
      },
      "disabled": false,
      "autoApprove": [
        "search_docs", "search_specs", "search_code", "search_logs",
        "list_projects", "list_files", "get_document", "get_project_summary",
        "find_function", "find_variable", "search_hex_pattern", "compare_projects",
        "add_project", "add_file", "add_folder", "add_pattern", "index_log_file"
      ]
    }
  }
}
```

`setup_mcp_config.py` generates any of these forms — see its `--uvx` / native-venv modes.

### Build a wheel locally

```bash
python scripts/sync_package_data.py   # keep src/rag_mcp/data/ in sync with config/
pip install build
python -m build --wheel --no-isolation
# Output: dist/pow_rag_mcp-1.0.0-py3-none-any.whl  (~85 KB)
```

`setup-pypi.bat` runs both steps automatically.


## Local release dry run

Build and validate the package locally before pushing a release tag — this catches packaging
mistakes (bad metadata, broken README rendering, an invalid version string) without consuming a
real release attempt. This sequence does **not** publish or upload anything anywhere; there is
no upload/publish command in it on purpose.

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

- `python -m build` produces both a wheel and a source distribution in `dist/`.
- `python -m twine check dist/*` validates the built artifacts' metadata and confirms the long
  description (this README) renders correctly — the same check the release workflow runs in CI
  before anything is uploaded.

If `twine check` reports an error, fix the underlying issue in `pyproject.toml` (or `README.md`)
and re-run both commands until it passes. Nothing is uploaded at any point in this procedure —
publishing only happens via the automated release workflow (pushing a `v<version>` tag).

## Post-publish verification

After a real release has been published to PyPI (via the automated release workflow), a
Maintainer should repeat these checks manually to confirm the release actually installs and runs
for end users. This mirrors the same checks the release workflow runs automatically, documented
here so they can be re-run by hand for any release.

### 1. `pip install` into a clean environment

```bash
python -m venv verify-env
# Windows: verify-env\Scripts\activate
# macOS/Linux: source verify-env/bin/activate
pip install pow-rag-mcp
```

**Expected:** `pip install` exits with a success status, and the `rag-mcp` console script is
resolvable on the environment's PATH.

### 2. Invoke the pip-installed console script

```bash
rag-mcp config
```

**Expected:** exits with a success status and produces no traceback.

### 3. `uvx --from pow-rag-mcp rag-mcp config`

```bash
uvx --from pow-rag-mcp rag-mcp config
```

**Expected:** exits with a success status, prints the resolved config and data paths, and
produces no traceback.

### 4. `uv tool install pow-rag-mcp`

```bash
uv tool install pow-rag-mcp
rag-mcp --help
```

**Expected:** `uv tool install` exits with a success status and installs a `rag-mcp` executable
on PATH; invoking that executable exits with a success status and produces no traceback.

---

If any of the four checks above fails, treat the release as failed and remediate (e.g. yank the
release on PyPI, or publish a patched version) — the package is already public on PyPI at this
point, so there is no automatic rollback.
