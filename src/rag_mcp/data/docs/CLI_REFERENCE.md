# CLI Reference

Full reference for the `rag-mcp` command-line tool (pip / uvx / `uv tool install` modes).
For Docker's `docker run ... indexer.py` / `server.py` equivalents, see
[DOCKER_GUIDE.md](DOCKER_GUIDE.md#cli-reference) instead — commands and flags are almost
identical, but invoked through `docker run` with volume mounts rather than directly.

```
rag-mcp <command> [options]
```

Commands: [`serve`](#serve) · [`index`](#index) · [`config`](#config) · [`docs`](#docs)

Running `rag-mcp` with no arguments is equivalent to `rag-mcp index`.

---

## `serve`

Starts the MCP server. Defaults to stdio transport (what Kiro / VS Code launch via `mcp.json`).

```bash
rag-mcp serve                  # stdio transport, background reindex on startup
rag-mcp serve --no-reindex     # stdio transport, skip background reindex
rag-mcp serve --http           # Streamable HTTP transport instead of stdio
rag-mcp serve --http --port 8080
rag-mcp serve --list-tools     # print the tool list and exit (health check)
```

| Flag | Effect |
|---|---|
| `--no-reindex` | Skip the background reindex thread on startup. Faster startup; index only reflects the last explicit `rag-mcp index` run. Recommended when the MCP client has a short connection timeout, or when multiple processes could write to the same index concurrently (e.g. a shared/hosted index). |
| `--http` | Run Streamable HTTP transport instead of stdio. For MCP clients that connect over HTTP (e.g. Odysseus) rather than launching a subprocess. |
| `--port N` | Port for `--http` mode (default: `8000`). If busy, auto-increments to the next free port in range `N`–`N+9` and logs the actual port used. |
| `--list-tools` | Print the available MCP tool list (name, params, first line of description) and exit. Useful as a health check without needing an MCP client. |

### Environment variables (serve)

| Variable | Effect |
|---|---|
| `RAG_CONFIG_PATH` | Explicit path to `config.yaml`. Overrides the default XDG location (see [`config`](#config)). Also used by Docker to point at the config seeded into the data volume. |
| `RAG_DATA_PATH` | Explicit ChromaDB storage directory. Overrides the default XDG data location. |
| `MCP_HTTP_PORT` | Overrides `--port` for `--http` mode and **disables** auto-fallback to another port — if the port is taken, startup fails immediately instead of silently binding elsewhere. Use this when something else (e.g. a container host port mapping) needs the port to match exactly. |
| `MCP_HTTP_PATH` | HTTP endpoint path for `--http` mode (default: `/mcp`). Example: `/rag-mcp`. |
| `HF_HUB_OFFLINE` | Set to `1` to suppress `sentence-transformers`'/`huggingface_hub`'s network reachability check on startup ("Warning: You are sending unauthenticated requests to the HF Hub..."). Safe once the embedding model and reranker are already cached locally (`~/.cache/huggingface/hub/`), which they are after the first successful run. `setup-pypi.bat` / `setup_mcp_config.py --uvx` / `remote_mcp_setup.py` set this by default in generated `mcp.json` entries (opt out with `remote_mcp_setup.py --no-hf-offline`). |

---

## `index`

Indexes configured projects into ChromaDB. Reads `config.yaml`, generates embeddings, stores
chunks. Safe to re-run — only changed files are re-processed (tracked via file hash).

```bash
rag-mcp index                             # index all configured projects
rag-mcp index --project NAME               # index only one project
rag-mcp index --reset                      # clear + full re-index (all projects)
rag-mcp index --reset --project NAME       # clear + full re-index (one project)
rag-mcp index --prune                      # remove chunks for files deleted from disk
rag-mcp index --estimate                   # dry run: estimate chunks/DB size, no writes
rag-mcp index --estimate --project NAME    # dry run for one project
rag-mcp index --convert-pdfs               # convert PDFs to Markdown for all projects
rag-mcp index --convert-pdfs --project NAME
rag-mcp index --convert-pdfs --path DIR    # convert PDFs in an arbitrary directory
rag-mcp index --add-project --name NAME --path DIR                # auto-detect + register a new project
rag-mcp index --add-folder --project NAME --path DIR [--pattern GLOB]   # add a folder to an existing project
rag-mcp index --add-pattern --project NAME --pattern GLOB [--type TYPE] [--description DESC]  # add a pattern to an existing project
```

| Flag | Effect |
|---|---|
| `--project NAME` | Restrict the operation to one project (by exact `name` in `config.yaml`). Combine with `--reset`, `--estimate`, `--convert-pdfs`, `--add-folder`, or `--add-pattern` (target project). |
| `--reset` | Delete the project's existing ChromaDB collection before indexing (full re-index instead of incremental). |
| `--prune` | Remove indexed chunks whose source file no longer exists on disk. Does not add or update anything. |
| `--estimate` | Scan and chunk files (no embeddings, no writes) to report file count, estimated chunk count, and projected DB size per project and in total. |
| `--convert-pdfs` | Convert PDF sources to Markdown ahead of indexing (normally done automatically, on demand, during indexing — this flag runs it standalone). Combine with `--project` to scope to one project, or `--path DIR` to convert an arbitrary directory instead of a configured project. |
| `--add-project` | Auto-detect the tech stack/file layout under `--path` (or the current directory if omitted) and append a new project entry to `config.yaml`. Requires `--name`. Does not index — run `rag-mcp index --project NAME` afterward. |
| `--add-folder` | Index all files matching `--pattern` (default `**/*`) inside `--path` into the existing project named by `--project`, and persist the resulting pattern in `config.yaml` for future re-indexing. `--path` must be inside that project's `base_path`. Indexes immediately (unlike `--add-project`). |
| `--add-pattern` | Add a glob `--pattern` (relative to the project's `base_path`) to the existing project named by `--project`, indexing any currently-matching files immediately. If no files match yet, the pattern is still persisted and picked up on a future run. Optional `--type` (`source`\|`header`\|`documentation`\|`config`, default `documentation`) and `--description`. |
| `--name NAME` | Project name — required with `--add-project`. |
| `--path DIR` | Directory path — used by `--add-project` (project root), `--add-folder` (folder to add), and `--convert-pdfs` (arbitrary directory instead of a configured project). |
| `--pattern GLOB` | Glob pattern — for `--add-folder` (relative to `--path`, default `**/*`) or `--add-pattern` (relative to the project's `base_path`, required). |
| `--type TYPE` | File type classification for `--add-pattern`: `source`, `header`, `documentation` (default), or `config`. |
| `--description DESC` | Human-readable description stored alongside the new source entry (for `--add-pattern`). |

> **Repo-checkout-only flags:** `--add-project`, `--add-folder`, `--add-pattern`, and
> `--convert-pdfs --path` are only available when running from the repo checkout
> (`python indexer.py ...`) or an editable install. In a real pip/uvx/`uv tool install` package
> with no `indexer.py` file on disk, the CLI falls back to an inline implementation that
> supports `--project`, `--reset`, `--prune`, and `--estimate` only. To add a project, folder, or
> pattern from a pure package install, use the equivalent MCP tools instead (`add_project`,
> `add_folder`, `add_pattern` — see [TOOLS_GUIDE.md](TOOLS_GUIDE.md) / `rag-mcp docs
> tools`), which work identically regardless of install mode since they go through the running
> server rather than the CLI.

---

## `config`

Shows the resolved config and data paths (no side effects beyond first-run seeding).

```bash
rag-mcp config          # print resolved config.yaml and data dir paths
rag-mcp config --init   # same as above; explicit no-op marker for scripting
```

On first run (if no config exists yet), a `config.yaml` is seeded from the bundled template at
the resolved path below. See [PIP_INSTALL_GUIDE.md](PIP_INSTALL_GUIDE.md#first-run--config-seeding)
for what happens if the bundled template can't be found (stub fallback).

| Platform | Default config path | Default data path |
|---|---|---|
| Windows | `%APPDATA%\rag-mcp\config.yaml` | `%LOCALAPPDATA%\rag-mcp\` |
| Linux / macOS | `~/.config/rag-mcp/config.yaml` | `~/.local/share/rag-mcp/` |

Both are overridable via `RAG_CONFIG_PATH` / `RAG_DATA_PATH` (see [`serve`](#serve) env vars above).

---

## `docs`

Lists or prints documentation bundled inside the installed package — works from any install
mode (pip, uvx, `uv tool install`) with no repo checkout needed.

```bash
rag-mcp docs                # list available bundled docs
rag-mcp docs tools          # print the full MCP tool reference
rag-mcp docs log-indexing   # print the log indexing usage guide
rag-mcp docs log-patterns   # print the log pattern configuration guide
```

| Name | Bundled doc | Also available at (repo) |
|---|---|---|
| `tools` | Full MCP tool reference (21 tools, usage examples) | [doc/TOOLS_GUIDE.md](TOOLS_GUIDE.md) |
| `log-indexing` | Structured log indexing usage guide | [doc/LOG_INDEXING_GUIDE.md](LOG_INDEXING_GUIDE.md) |
| `log-patterns` | How to write log pattern configs for new formats | [doc/LOG_PATTERN_CONFIGURATION.md](LOG_PATTERN_CONFIGURATION.md) |

Only these three are bundled — setup/architecture docs (this file, `DOCKER_GUIDE.md`,
`PIP_INSTALL_GUIDE.md`, `ARCHITECTURE.md`, `TROUBLESHOOTING.md`) are repo-only, since they're
primarily useful before or during installation rather than at runtime. See the Documentation
table in [README.md](../README.md#documentation) for the full split.

Output is written as raw UTF-8 regardless of the terminal's console codepage (some docs contain
non-ASCII characters like `→`, which crash Python's default `print()` on Windows consoles using
`cp1252`).

---

## Exit codes

All commands exit `0` on success. Non-zero (`1`, typically) on: missing/invalid config, unknown
project name passed to `--project`, unknown `docs` name, or unhandled exceptions during indexing.
`serve` runs indefinitely (exit code only applies if it fails to start).
