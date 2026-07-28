# Docker Guide (Phase 1)

Full setup, project management, and CLI reference for running rag-mcp in Docker.
For the pip-install alternative, see [PIP_INSTALL_GUIDE.md](PIP_INSTALL_GUIDE.md).

## Setup

Requires Docker Desktop.

### 1. Build the image

```bash
cd <your-checkout>/pow-mcp-rag-new
docker build -t rag-mcp-new-pip:latest .
```

### 2. Index your projects

Run the indexer container once to build the index into the `rag-mcp-new-pip-data` volume.
Set `SRC` to the host folder that holds your repos:

```powershell
# PowerShell
$SRC = "C:/Users/you/GIT"
docker run --rm -v "${SRC}:/projects:ro" -v rag-mcp-new-pip-data:/app/data rag-mcp-new-pip:latest python indexer.py
```

```bash
# bash
SRC=/host/git
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data rag-mcp-new-pip:latest python indexer.py
```

### 3. Configure Kiro MCP

Add the following to `~/.kiro/settings/mcp.json` (replace the path with your actual repos folder):

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
        "add_project", "add_file", "add_folder", "add_pattern",
        "index_log_file"
      ]
    }
  }
}
```

Restart Kiro — the RAG is ready.

> **MCP server name:** The key in `mcp.json` defaults to `rag-mcp` (from `config/server_info.json`).
> To use a custom name, run `setup-docker.bat --server-name my-rag` or pass `--server-name` to
> `scripts/setup_mcp_config.py` directly.

> **Why `--no-reindex`?** The ChromaDB index lives in the shared `rag-mcp-new-pip-data` volume.
> If the server auto-reindexed on every Kiro startup, concurrent writers would risk
> corrupting the index. Indexing is an explicit, separate step — see [Managing the Index](#managing-the-index).

### What gets indexed

File types are configured in `config.yaml` (stored inside the `rag-mcp-new-pip-data` volume) under
`index_extensions`. By default: C/C++, Python, React/JS/TS, C#, Go, Kotlin, Markdown, PDF, text.
Directories listed in `excluded_dirs` (build, node_modules, `.git`, ...) are skipped.

## Managing the Index

All indexer commands run a one-off container against the `rag-mcp-new-pip-data` volume. The running
server never writes to the volume, so indexer containers can run safely while the server is up —
just avoid running two indexer containers simultaneously.

See the [CLI Reference](#cli-reference) below for all available commands.

## Adding a New Project

### From Kiro chat (easiest)

Ask the AI to add it:
> "Add the my-service project to the RAG, it's at C:/Users/me/GIT/my-service"

The AI calls the `add_project` MCP tool, which builds source patterns from the configured
`index_extensions`, updates `config.yaml` inside the volume, and indexes immediately — no
terminal needed.

### From the command line

See `--add-project` in the [CLI Reference](#cli-reference).

Both `add_project` and the CLI use the same rule: for the folder, add a recursive `**/*.ext`
pattern for every configured `index_extensions` type that exists under it, skipping `excluded_dirs`.

### Adding a specific file pattern to an existing project

Use `add_pattern` when you want to index a set of files by glob pattern without pointing at
a concrete folder path. The pattern is relative to the project's `base_path`:

> "Add the pattern `doc/specifications/vendor-x/**/*.md` to project `my-service`, type documentation, description 'Vendor X hardware specs'"

The AI calls `add_pattern`, which registers the pattern in `config.yaml` and indexes matching
files immediately. If no files match yet, the pattern is still persisted and will be picked up
on the next indexer run when files appear.

### Removing a project

Use the MCP tools from Kiro chat (easiest):
> "Remove the my-service project from the RAG"
> "Clear the index for my-service" (keeps it in config)

These call `remove_project` / `clear_project_index` against the running server, which writes
to the shared volume. Alternatively, edit `config.yaml` inside the volume directly (see
[Configure projects](#configure-projects)), then reset.

## Configure projects

Projects are defined in `config.yaml` stored inside the `rag-mcp-new-pip-data` volume at
`/app/data/config.yaml`. The `base_path` values use container paths (under `/projects`):

```yaml
base_path: "/projects/my-service"
```

`config.template.yaml` in the repo is the starting point — it is seeded into the volume on
first run by the container entrypoint. The live config in the volume is machine-specific and
is never committed to git.

To edit the config directly, open a shell into a temporary container with the volume mounted:

```bash
docker run --rm -it -v rag-mcp-new-pip-data:/app/data alpine sh
# then: vi /app/data/config.yaml
```

## Use in Kiro

Once configured, 21 MCP tools are available for searching, browsing, and managing your indexed
projects. See **[TOOLS_GUIDE.md](TOOLS_GUIDE.md)** for the full list with usage examples.

Key tools for log analysis:
- `index_log_file(file, time_from?, time_to?)` — Index a log file on demand (streams large files in batches)
- `search_logs(query?, severity?, time_range_start?, ...)` — Search indexed log events with rich filtering

> **Note on `cancel_indexing`:** This tool exists but is not useful in Docker mode. The server
> runs with `--no-reindex` so no background reindex ever starts, and MCP tools are synchronous —
> `cancel_indexing` cannot interrupt a running `index_log_file` call from another thread.
> To stop a long-running `index_log_file`, restart the MCP server from Kiro's MCP panel.

### Re-index from Kiro

A "Re-index RAG" hook is available in the Agent Hooks panel. Click it to refresh the index
after making changes — no terminal needed.

## HTTP Server (for Odysseus or any HTTP MCP client)

Odysseus runs in Docker and connects to MCP servers over HTTP — not stdio. Start the HTTP
server using the helper script or compose:

### `start-http.ps1` (recommended — handles port conflicts automatically)

```powershell
.\start-http.ps1                              # start on first free port from 8000
.\start-http.ps1 -StartPort 8080              # try from 8080 upward
.\start-http.ps1 -Name my-rag                 # custom container name
.\start-http.ps1 -UpdateMcp                   # start + write URL into mcp.json
.\start-http.ps1 -UpdateMcp -ServerName rag   # start + update mcp.json under custom key
```

The script probes host ports from `StartPort` upward until it finds a free one, then passes the
chosen port to both `-p` (Docker host mapping) and `-e MCP_HTTP_PORT` (container binding) so
both sides always agree. Output:

```
[probe] Port 8000 in use -- using 8001 instead
[docker] Starting 'rag-mcp-new-pip-http' on http://localhost:8001/mcp
[done] Server running at http://localhost:8001/mcp
[mcp.json] Updated 'rag-mcp' -> http://localhost:8001/mcp
[mcp.json] Restart Kiro to apply.
```

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `-StartPort` | `8000` | First port to probe |
| `-MaxTries` | `10` | How many ports to try before giving up |
| `-Name` | `rag-mcp-new-pip-http` | Docker container name |
| `-SRC` | `C:/GIT` | Host folder mounted as `/projects` |
| `-HttpPath` | `/mcp` | HTTP endpoint path (sets `MCP_HTTP_PATH` in container) |
| `-ServerName` | *(from server_info.json)* | MCP key in `mcp.json` (default: `rag-mcp`) |
| `-UpdateMcp` | *(switch)* | If set, writes the HTTP URL into `~/.kiro/settings/mcp.json` |

### docker compose (fixed port 8000)

```powershell
$env:PROJECTS_DIR = "C:/Users/you/GIT"
docker compose up -d server-http
```

If port 8000 is already taken, use `start-http.ps1` instead.

### Stop the HTTP server

```powershell
docker stop rag-mcp-new-pip-http
docker rm rag-mcp-new-pip-http
# or via compose:
docker compose down server-http
```

### Connect from Odysseus

Add the RAG server to Odysseus's MCP configuration:

```
http://host.docker.internal:8000/mcp
```

> Use `host.docker.internal` if Odysseus runs in its own Docker Compose stack.
> Use `rag-mcp-new-pip-http:8000` if both stacks share the same Docker network.
> Use `localhost:8000` from the host machine or any non-containerized MCP client.

The HTTP server uses **Streamable HTTP transport** (the current MCP standard), which Odysseus
and other modern MCP clients support natively. The `--no-reindex` flag is recommended to avoid
startup delays — run the indexer separately when you want to refresh the index.

## Log Indexing

The RAG server supports structured log indexing — parsing device communication logs into
searchable events with rich metadata (severity, timestamps, error codes, device IDs, event types).
The pipeline is fully generic: parsing rules, filters, and grouping are all driven by YAML
configuration, so any log format can be supported by writing a new pattern config — no code
changes required.

See **[LOG_INDEXING_GUIDE.md](LOG_INDEXING_GUIDE.md)** for the usage guide and
**[LOG_PATTERN_CONFIGURATION.md](LOG_PATTERN_CONFIGURATION.md)** for how to write pattern
configs for new log formats, including a full walkthrough you can use as a template.

## CLI Reference

All commands assume:
- `SRC` = host folder containing your repos (e.g. `C:/Users/you/GIT`)
- `rag-mcp-new-pip-data` = Docker named volume holding the index and config
- `rag-mcp-new-pip:latest` = the built image

Set `SRC` before running:

```powershell
# PowerShell
$SRC = "C:/Users/you/GIT"
```
```bash
# bash
SRC=/host/git
```

### Build

```bash
docker build -t rag-mcp-new-pip:latest .
```

### Indexer

> **Before using `--project <NAME>`:** the project names inside the `rag-mcp-new-pip-data`
> volume are the source of truth — they can drift from any local `config.yaml` you have
> on disk (e.g. after projects were added/removed via MCP tools like `add_project`).
> Always confirm the exact name first with one of these:
>
> ```bash
> # Option A — if the MCP server is connected in Kiro, just ask the agent to run
> # list_projects(). This reads the live volume directly.
>
> # Option B — query the volume's ChromaDB directly, no running server needed:
> docker run --rm -v "rag-mcp-new-pip-data:/app/data" \
>   rag-mcp-new-pip:latest python -c "import chromadb; c = chromadb.PersistentClient(path='/app/data'); print([col.name for col in c.list_collections()])"
> ```
>
> Reindexing with a `<NAME>` that doesn't exactly match an existing project is a silent
> no-op — the command exits successfully but nothing is reindexed. There is no error.

```bash
# Index all configured projects (incremental — only changed files)
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py

# Index one project (confirm the exact name first — see note above)
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --project <NAME>

# Reset (clear + full re-index) — all projects
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --reset

# Reset one project
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --reset --project <NAME>

# Prune chunks for files deleted from disk
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --prune

# Estimate index size (dry run — no writes)
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --estimate

# Add a new project
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --add-project --name "<NAME>" --path "/projects/<repo>"
```

### Server

```bash
# List available MCP tools and exit (useful for health-checking; no source mount needed)
docker run -i --rm -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python server.py --list-tools

# Run server manually over stdio (normally launched by the MCP client)
docker run -i --rm \
  -v "$SRC:/projects:ro" \
  -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python server.py --no-reindex

# Run server over HTTP — for container-to-container clients (e.g. Odysseus)
# Use start-http.ps1 (recommended) for automatic port conflict handling:
#   .\start-http.ps1
# Or manually with a fixed port:
docker run -d --name rag-mcp-new-pip-http \
  -v "$SRC:/projects:ro" \
  -v rag-mcp-new-pip-data:/app/data \
  -p 8000:8000 \
  -e MCP_HTTP_PORT=8000 \
  --restart unless-stopped \
  rag-mcp-new-pip:latest python server.py --http --no-reindex
# Connects at: http://localhost:8000/mcp  (host)
# Or via compose: docker compose up -d server-http
```

### Server flags

| Flag | Effect |
|------|--------|
| `--no-reindex` | Skip background reindex on startup (recommended for Docker — use explicit indexer runs instead) |
| `--http` | Run Streamable HTTP transport instead of stdio (for Odysseus and other HTTP MCP clients) |
| `--port N` | Port for `--http` mode (default: 8000). Auto-increments to next free port if busy. |
| `--lock-timeout N` | Max wait time (seconds) for embedding lock during reindex (default: 30) |
| `--list-tools` | Print available tools and exit |

`MCP_HTTP_PORT` env var overrides `--port` and disables auto-fallback (use this in Docker where
the host port mapping must match exactly — if the port is taken the container will fail fast
rather than silently bind to a different port that Docker isn't forwarding).
Use `start-http.ps1` to handle port conflicts automatically on the host side.

`MCP_HTTP_PATH` env var sets the HTTP endpoint path (default: `/mcp`). Use this to expose the
server at a custom path such as `/rag-mcp`:

```powershell
# docker run with custom path
docker run -d ... -e MCP_HTTP_PORT=8000 -e MCP_HTTP_PATH=/rag-mcp rag-mcp-new-pip:latest python server.py --http --no-reindex

# start-http.ps1 with custom path
.\start-http.ps1 -HttpPath /rag-mcp -UpdateMcp

# docker compose with custom path
$env:MCP_HTTP_PATH = "/rag-mcp"
docker compose up -d server-http
```

### Volume management

```bash
# List all Docker volumes
docker volume ls

# Wipe the entire index and config (start over)
docker volume rm rag-mcp-new-pip-data
```

### docker compose shortcuts

```bash
# Re-index via compose (set PROJECTS_DIR first)
#   PowerShell: $env:PROJECTS_DIR = "C:/Users/you/GIT"
#   bash:       export PROJECTS_DIR=/host/git
docker compose run --rm indexer

# One project via compose (override the default command)
docker compose run --rm indexer python indexer.py --project <NAME>
```

### Auto-detection signals (used by `--add-project`)

| Language / Stack | Detection signal |
|---|---|
| C/C++ | CMakeLists.txt, src/, include/, component/, C-Fontes/ |
| Python | `*.py`, pyproject.toml, setup.py |
| Go | go.mod |
| C# | `*.csproj`, `*.sln` |
| Node.js/TypeScript | package.json, tsconfig.json |
