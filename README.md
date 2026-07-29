# pow-mcp-rag-new

A local RAG (Retrieval-Augmented Generation) MCP server for project documentation and code.

## Overview

This project provides a Model Context Protocol (MCP) server that indexes project documentation
(specs, headers, source files, PDFs, configs) into a local vector database (ChromaDB) and exposes
semantic search tools to Kiro or any MCP-compatible client.

Supports multiple tech stacks: C/C++, Python, Go, C#, Node.js/TypeScript.

Three deployment modes:
- **Docker** (Phase 1) — zero local Python needed; server runs in a container
- **Local PyPI + uvx** (Phase 2a) — install `rag-mcp` via `uvx` from a local package index; no persistent venv, easiest to keep updated. Stepping stone toward a hosted index.
- **pip install** (Phase 2b) — install `rag-mcp` directly into a Python environment.

---

## Quick Start

### Local PyPI + uvx (recommended for pip-mode)

```bash
cd <your-checkout>/pow-mcp-rag-new
setup-pypi.bat
```

This builds the wheel, publishes it to a local `pypiserver` index (`packages/`), installs it as a
persistent `uv tool` (a stable exe at `~/.local/bin/rag-mcp.exe` — resolved once, not on
every launch), and wires up `~/.kiro/settings/mcp.json` + `.vscode/mcp.json` to launch it directly.
No Docker, no persistent venv to manage. See [TROUBLESHOOTING.md](doc/TROUBLESHOOTING.md) for why
this is preferred over plain `uvx --from` on Windows with this package's large dependency tree.

**Full guide:** [doc/PIP_INSTALL_GUIDE.md](doc/PIP_INSTALL_GUIDE.md#local-pypi-uvx-mode)
(keep the local index running, rebuild/republish after code changes, migrating to S3 later).

### Docker (alternative)

```bash
cd <your-checkout>/pow-mcp-rag-new
docker build -t rag-mcp-new-pip:latest .

# Index your projects (set SRC to your repos folder)
$SRC = "C:/Users/you/GIT"   # PowerShell
docker run --rm -v "${SRC}:/projects:ro" -v rag-mcp-new-pip-data:/app/data rag-mcp-new-pip:latest python indexer.py
```

Then add to `~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "rag-mcp": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "C:/Users/you/GIT:/projects:ro",
        "-v", "rag-mcp-new-pip-data:/app/data",
        "rag-mcp-new-pip:latest",
        "python", "server.py", "--no-reindex"
      ],
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

Restart Kiro — the RAG is ready. **Full guide:** [doc/DOCKER_GUIDE.md](doc/DOCKER_GUIDE.md)
(project management, HTTP server, `docker run` CLI reference, `setup-docker.bat` automation).

### pip install (hosted index, once available)

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install rag-mcp --extra-index-url https://<your-s3-bucket>.s3.<region>.amazonaws.com/
rag-mcp config   # seeds config on first run, shows resolved paths
rag-mcp index
rag-mcp serve
```

**Full guide:** [doc/PIP_INSTALL_GUIDE.md](doc/PIP_INSTALL_GUIDE.md)

---

## Documentation

> **Reading this from `pip show` / a package-index page instead of the repo?** This README's
> links are relative paths into the `pow-mcp-rag-new` repo (GitHub/clone) and won't resolve from
> an installed package alone. Three docs travel with the install and are available offline via
> `rag-mcp docs <name>` (see table below); the rest require the repo checkout.

| Guide | Covers | Bundled in package? |
|---|---|---|
| [doc/DOCKER_GUIDE.md](doc/DOCKER_GUIDE.md) | Full Docker setup, adding/removing projects, HTTP server, complete CLI reference (Docker mode) | No — repo only |
| [doc/PIP_INSTALL_GUIDE.md](doc/PIP_INSTALL_GUIDE.md) | Local PyPI + uvx setup, pip installation, config seeding, building/publishing the wheel, migrating to S3/CodeArtifact | No — repo only |
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | How indexing/retrieval works, embedding model, PDF handling, file structure | No — repo only |
| [doc/CLI_REFERENCE.md](doc/CLI_REFERENCE.md) | Full `rag-mcp` CLI reference: `serve`/`index`/`config`/`docs`, all flags and env vars | **Yes** — `rag-mcp docs cli` |
| [doc/TOOLS_GUIDE.md](doc/TOOLS_GUIDE.md) | Full MCP tool reference (21 tools) with usage examples | **Yes** — `rag-mcp docs tools` |
| [doc/LOG_INDEXING_GUIDE.md](doc/LOG_INDEXING_GUIDE.md) | Structured log indexing usage guide | **Yes** — `rag-mcp docs log-indexing` |
| [doc/LOG_PATTERN_CONFIGURATION.md](doc/LOG_PATTERN_CONFIGURATION.md) | How to write pattern configs for new log formats | **Yes** — `rag-mcp docs log-patterns` |
| [doc/TROUBLESHOOTING.md](doc/TROUBLESHOOTING.md) | Common issues and fixes | No — repo only |

Run `rag-mcp docs` with no arguments to list the bundled docs from any install (pip, uvx,
or `uv tool install`) without needing the repo.

## What gets indexed

File types are configured in `config.yaml` under `index_extensions`. By default: C/C++, Python,
React/JS/TS, C#, Go, Kotlin, Markdown, PDF, text. Directories in `excluded_dirs` (build,
node_modules, `.git`, ...) are skipped. See [doc/DOCKER_GUIDE.md](doc/DOCKER_GUIDE.md#what-gets-indexed)
for details on configuring projects and adding new sources.

## MCP Tools

Once configured, 21 MCP tools are available for searching, browsing, and managing your indexed
projects — see **[doc/TOOLS_GUIDE.md](doc/TOOLS_GUIDE.md)** for the full list with examples
(also available offline: `rag-mcp docs tools`).

Key tools:
- `search_docs` / `search_specs` / `search_code` — semantic search, scoped by file type
- `search_hex_pattern`, `find_function`, `find_variable` — exact text matching for codes/symbols
- `search_logs`, `index_log_file` — structured log search and on-demand indexing
- `add_project`, `add_pattern`, `remove_project` — index management from Kiro chat

## Log Indexing

Structured log indexing with severity filtering, time-window search, and hex error-code
matching. The pipeline is fully generic — driven by YAML pattern configs, so any log format
can be supported without code changes. See [doc/DOCKER_GUIDE.md](doc/DOCKER_GUIDE.md#log-indexing)
and [doc/LOG_INDEXING_GUIDE.md](doc/LOG_INDEXING_GUIDE.md) (also: `rag-mcp docs log-indexing`).

## Need Help?

See [doc/TROUBLESHOOTING.md](doc/TROUBLESHOOTING.md) for common issues, including:
- MCP server hangs on startup
- Stale search results after re-indexing
- `entrypoint.sh` exec errors from CRLF line endings
- `--project <NAME>` silently doing nothing
- Concurrent query errors
