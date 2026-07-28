# pip / uvx Install Guide (Phase 2)

Install `rag-mcp` without Docker. Two flavors:

- **Local PyPI + uvx** (recommended) — no persistent venv, easiest to keep updated, and a direct
  stepping stone toward a hosted index (S3 or AWS CodeArtifact) later.
- **Direct pip install** — into a plain venv, from a local wheel or a hosted index once published.

For the Docker alternative, see [DOCKER_GUIDE.md](DOCKER_GUIDE.md).

---

## Local PyPI + uvx mode

This runs a minimal PyPI-compatible index (`pypiserver`) on your machine, serving the
`rag-mcp` wheel from a `packages/` folder in this repo. `uv`/`uvx` install and run the
package from that index in an isolated, cached environment — no venv to manage, no Docker.

> **Windows note:** `setup-pypi.bat` defaults to `uv tool install` (a one-time resolve into a
> persistent exe at `~/.local/bin/rag-mcp.exe`) rather than `uvx --from` (which re-resolves
> the ~110-package dependency tree on every single MCP connection). The repeated resolution
> intermittently races Windows Defender's real-time scanner on `uv`'s trampoline `.exe` write,
> failing with `Failed to update Windows PE resources... Acesso negado`. See
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#uvx-install-fails-with-failed-to-update-windows-pe-resources-acesso-negado)
> for details. `uv tool install` avoids the problem entirely by only resolving once.

### 1. One-command setup

```bash
cd <your-checkout>/pow-mcp-rag-new
setup-pypi.bat
```

This:
1. Creates a small build-only venv (`.venv/`) with `build` + `pypiserver` (not used at runtime)
2. Syncs `config/*.yaml|json` into `src/rag_mcp/data/` so they're bundled inside the wheel
3. Builds the wheel (`python -m build --wheel`) into `dist/`
4. Copies the wheel into `packages/` (the local index's package folder)
5. Runs `uv tool install --extra-index-url ... --force rag-mcp` — resolves the dependency
   tree once and installs a stable exe at `~/.local/bin/rag-mcp.exe`
6. Writes/updates the `rag-mcp` entry in `~/.kiro/settings/mcp.json` (if `~/.kiro` exists)
   and `.vscode/mcp.json`, both pointing directly at that exe (falls back to `uvx --from` if
   `uv tool install` isn't available or fails)

### 2. Start the local index

The setup script does **not** keep the index server running (it's a one-shot build). Start it
yourself and leave it running while Kiro / VS Code are connected:

```powershell
.venv\Scripts\python.exe -m pypiserver run -p 8080 packages --disable-fallback
```

Verify it's serving the package:

```powershell
Invoke-WebRequest -Uri "http://localhost:8080/simple/rag-mcp/" -UseBasicParsing
```

> **Tip:** Run this as a background/startup task (Task Scheduler, or `control_pwsh_process`-style
> background terminal in Kiro) so it survives reboots without manual intervention.

### 3. Restart Kiro / reconnect the MCP server

With the default `--stable` mode, the server starts instantly (no dependency resolution — it
already happened once during `setup-pypi.bat`). If `mcp.json` falls back to `uvx --from` (stable
install unavailable), the first connection downloads and caches all dependencies (~10-30s, ~110
packages including torch/chromadb); subsequent connections reuse `uv`'s cache and start in ~1-2s
— but every launch still re-resolves the tree, which is what triggers the intermittent Windows
Defender race described above.

### 4. Index your projects

```bash
$env:RAG_CONFIG_PATH = "D:/GitHub/pow-mcp-rag-new/config/config.yaml"   # or use the seeded XDG path
uvx --extra-index-url http://localhost:8080/simple/ --from rag-mcp rag-mcp index
```

Or ask Kiro chat to add/index projects once the MCP server is connected (see
[TOOLS_GUIDE.md](TOOLS_GUIDE.md) — `add_project`, `add_pattern`).

### Rebuild and republish after code changes

Re-run `setup-pypi.bat` any time you change server code — it rebuilds the wheel, overwrites the
file in `packages/`, and re-runs `uv tool install --force` so the persistent exe picks up the new
build immediately (no manual reinstall step, no mcp.json changes needed since the exe path is
stable).

If managing the stable install manually instead of via the script:

```bash
uv tool install --extra-index-url http://localhost:8080/simple/ --force rag-mcp
```

> Bump `version` in `pyproject.toml` when publishing a new build so cache invalidation is
> unambiguous — reusing the same version number with different content can serve stale cached
> wheels in some edge cases, for both `uv tool install` and `uvx --from`.

### Migrating to a hosted index (S3 / CodeArtifact) later

Only the index URL changes — the wheel format, build process, and `uvx` command stay identical:

```bash
# Local (today):
uvx --extra-index-url http://localhost:8080/simple/ --from rag-mcp rag-mcp serve

# S3-hosted static index (later):
uvx --extra-index-url https://<bucket>.s3.<region>.amazonaws.com/simple/ --from rag-mcp rag-mcp serve

# AWS CodeArtifact (managed, alternative to S3):
uvx --extra-index-url https://<domain>-<account>.d.codeartifact.<region>.amazonaws.com/pypi/<repo>/simple/ --from rag-mcp rag-mcp serve
```

For an S3 static index, generate the index HTML with a tool like [`dumb-pypi`](https://github.com/chriskuehl/dumb-pypi)
and `aws s3 sync` the `packages/` + generated index pages to the bucket. `setup_mcp_config.py --uvx`
already accepts any `--index-url`, so re-running it with the new URL is all that's needed to
update `mcp.json` / `.vscode/mcp.json`.

---

## Configuring a remote machine (no repo checkout)

`scripts/setup_mcp_config.py` (used by `setup-pypi.bat`) needs the full repo — it reads
`config/server_info.json` and defaults `.vscode/mcp.json` to the repo root. For a machine that
only needs to *consume* an already-published index (local or hosted) without cloning
`pow-mcp-rag-new` at all, use `scripts/remote_mcp_setup.py` instead — it has zero dependencies
beyond the Python standard library and can run as a single file:

```bash
# 1. Install the package as a persistent tool (resolves once, avoids the
#    per-launch Windows Defender trampoline-exe race — see TROUBLESHOOTING.md)
uv tool install --extra-index-url <INDEX_URL> rag-mcp

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

## Direct pip install (alternative to uvx)

### Install from the local wheel

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install dist/rag_mcp-1.0.0-py3-none-any.whl
```

### Install from the local PyPI index

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install rag-mcp --extra-index-url http://localhost:8080/simple/
```

### Install from a hosted S3 index (once published)

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install rag-mcp --extra-index-url https://<your-s3-bucket>.s3.<region>.amazonaws.com/
```

## First run — config seeding

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

## Configure projects

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

## Index and serve

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

## MCP config

`setup-pypi.bat` writes both of these automatically (in `--stable` mode by default — see the
Windows note above). Shown here for reference / manual setup.

### Kiro (`~/.kiro/settings/mcp.json`) — stable tool exe (default, recommended on Windows)

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

Install/update the exe with: `uv tool install --extra-index-url <index_url> --force rag-mcp`

> **`HF_HUB_OFFLINE=1`:** Suppresses `sentence-transformers`'/`huggingface_hub`'s network
> reachability check on every startup ("Warning: You are sending unauthenticated requests to the
> HF Hub..."). Safe once the embedding model (`BAAI/bge-small-en-v1.5`) and reranker
> (`cross-encoder/ms-marco-MiniLM-L-6-v2`) are already cached locally (they are, after the first
> successful run) — `sentence-transformers` loads straight from `~/.cache/huggingface/hub/`
> without touching the network. If you later switch to a model that isn't cached yet, temporarily
> remove this env var (or delete it from `mcp.json`) so the first download can happen, then add
> it back.

### Kiro — `uvx --from` (fallback, re-resolves on every launch)

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "uvx",
      "args": [
        "--extra-index-url", "http://localhost:8080/simple/",
        "--from", "rag-mcp",
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

### VS Code / VS 2026 (`.vscode/mcp.json`) — stable tool exe

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

## Build a wheel locally

```bash
python scripts/sync_package_data.py   # keep src/rag_mcp/data/ in sync with config/
pip install build
python -m build --wheel --no-isolation
# Output: dist/rag_mcp-1.0.0-py3-none-any.whl  (~85 KB)
```

`setup-pypi.bat` runs both steps automatically.

## Publish to S3 (next phase)

```bash
# Upload wheel to S3 (acts as a simple find-links PyPI index)
aws s3 cp dist/ s3://<your-bucket>/rag-mcp/ --recursive

# Users install with:
pip install rag-mcp --extra-index-url https://<bucket>.s3.<region>.amazonaws.com/
# or, with uvx:
uvx --extra-index-url https://<bucket>.s3.<region>.amazonaws.com/ --from rag-mcp rag-mcp serve
```

For a proper PEP 503 "simple" index (required for correct version resolution beyond a single
wheel), generate index pages with [`dumb-pypi`](https://github.com/chriskuehl/dumb-pypi) before
syncing to S3, rather than relying on S3's raw directory listing.
