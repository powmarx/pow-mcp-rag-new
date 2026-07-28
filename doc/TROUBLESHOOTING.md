# Troubleshooting

## Diagnosing ChromaDB / indexing problems

Before digging into a specific symptom below, run the diagnostic script — it catches most of
the issues on this page automatically (sqlite corruption, config/collection drift, corrupted
HNSW vector indexes) without needing to reproduce the failure manually.

```powershell
.\scripts\diagnose-chromadb.ps1                                   # full scan, all projects
.\scripts\diagnose-chromadb.ps1 -Project my-project
.\scripts\diagnose-chromadb.ps1 -SkipProbe                        # fast, skips count/query checks
```

It runs read-only checks against the live `rag-mcp-new-pip-data` volume (safe to run while Kiro is
connected) and reports:

- SQLite integrity of `chroma.sqlite3`.
- Collections in `config.yaml` that don't exist in ChromaDB (see "`--project` silently does
  nothing" below).
- Missing/empty HNSW index files on disk.
- Per-collection `count()`/`query()` probes, each run in an isolated subprocess — if a
  collection's vector index is corrupted, it crashes that subprocess (not the whole scan) and
  the script reports which project to reset. This is the same crash pattern behind the
  "Two indexer containers running at the same time" issue below: it manifests as the MCP
  server (or any `docker run ... server.py`) exiting with code 139 (SIGSEGV) shortly after
  `[startup] ChromaDB connected`, the moment it tries to count/read the corrupted collection.

Exit code is `0` if healthy, `1` if any issue was found. The underlying Python script is at
`scripts/diagnose_chromadb.py` if you want to run it directly inside a container.

## MCP server hangs on startup (ChromaDB won't connect)

**Symptom:** The `rag-mcp-new-pip-mcp` MCP server never finishes starting. The log shows
`Embedding model configured` but never reaches `ChromaDB connected`. Other database tools
(e.g., SQL Server Management Studio) may also hang simultaneously.

**Cause:** The Windows Management Instrumentation (WMI) service can enter a stuck state that
blocks SQLite file I/O system-wide. Since ChromaDB uses SQLite internally, it hangs during
`PersistentClient` initialization.

**Fix:** Run `restart_wmi.bat` as Administrator:

```
right-click restart_wmi.bat → "Run as administrator"
```

Then reconnect the MCP server in Kiro (or restart Kiro).

**Verification:** Confirm the server starts correctly (no source mount needed for this check):

```bash
docker run -i --rm -v rag-mcp-new-pip-data:/app/data rag-mcp-new-pip:latest python server.py --list-tools
```

If it prints the tool list and exits cleanly, the server is healthy.

## Stale search results after re-indexing

If Kiro returns outdated results after running the indexer, reconnect the MCP server from the
MCP panel in Kiro (no restart needed). The server reads the volume live and picks up index
changes immediately on reconnect.

## Two indexer containers running at the same time

Running concurrent indexer containers against the same volume can corrupt the ChromaDB index.
Symptom is usually the server crashing with exit code 139 (SIGSEGV) right after
`[startup] ChromaDB connected`. Run `.\scripts\diagnose-chromadb.ps1` to confirm which project's
collection is corrupted, then reset it:

```bash
docker run --rm -v "$SRC:/projects:ro" -v rag-mcp-new-pip-data:/app/data \
  rag-mcp-new-pip:latest python indexer.py --reset --project <NAME>
```

## `exec /usr/local/bin/entrypoint.sh: no such file or directory`

**Symptom:** `docker build` succeeds, but any `docker run` against the image fails immediately
with this exec error (seen right after `setup-docker.bat` cloning on a fresh Windows machine).

**Cause:** Git converted `docker/entrypoint.sh` (and other shell scripts) to CRLF line endings
on checkout. A Linux container reads the shebang line as `#!/bin/sh\r`, which doesn't match any
interpreter, so `exec` fails.

**Fix:** The repo ships a `.gitattributes` that forces LF for `*.sh` and other Linux-relevant
files regardless of the local git `autocrlf` setting. If you cloned before this was added, or
your git config overrode it, re-normalize the working tree:

```bash
git rm --cached -r .
git reset --hard HEAD
```

Then rebuild: `docker build -t rag-mcp-new-pip:latest .`

## `--project <NAME>` silently does nothing

**Symptom:** `indexer.py --project <NAME>` (or the "single project" reindex hook) exits
successfully but the project isn't actually reindexed.

**Cause:** The project name doesn't exactly match a project inside the `rag-mcp-new-pip-data` volume.
This is a silent no-op — there is no error message. Project names inside the volume are the
source of truth and can drift from any local `config.yaml` on disk (e.g. after projects were
added/removed via MCP tools like `add_project`, which write directly into the volume).

**Fix:** Confirm the exact name first:

```bash
# Option A — if the MCP server is connected in Kiro, ask the agent to call list_projects().
# This reads the live volume directly.

# Option B — run .\scripts\diagnose-chromadb.ps1, which reports config/collection drift.

# Option C — query the volume's ChromaDB directly, no running server needed:
docker run --rm -v "rag-mcp-new-pip-data:/app/data" \
  rag-mcp-new-pip:latest python -c "import chromadb; c = chromadb.PersistentClient(path='/app/data'); print([col.name for col in c.list_collections()])"
```

## `Error encoding query: Already borrowed`

**Symptom:** A search tool call fails with this error, usually when two queries run
concurrently (e.g. `search_docs` and `search_specs` in the same turn).

**Cause:** The `sentence-transformers` model is not thread-safe for concurrent `encode()` calls.
Running two encode operations in parallel from different threads triggers a PyTorch tensor
borrow-checker error.

**Fix:** Avoid issuing multiple search tool calls in the same parallel batch. Run them
sequentially instead.

## `uvx` install fails with "Failed to update Windows PE resources... Acesso negado"

**Symptom:** Running `uvx --from rag-mcp-new-pip-mcp rag-mcp-new-pip-mcp ...` (via local PyPI index or a
hosted one) fails partway through dependency installation with an error like:

```
error: Failed to install: pybase64-1.4.3-cp313-cp313-win_amd64.whl (pybase64==1.4.3)
  Caused by: Failed to update Windows PE resources: ...\uv-trampoline-XXXXX.exe
  Caused by: Acesso negado. (os error -2147024891)
```

The specific package name in the error varies between runs (`torch`, `pymupdf`, `mcp`,
`jsonschema`, etc.) — it's not tied to any particular dependency.

**Cause:** Windows Defender (or another antivirus/EDR agent) transiently locks `uv`'s
trampoline `.exe` files while they're being written to `%TEMP%`, racing with `uv`'s own
install step. This is an environment-level file-lock race, not a bug in the package or its
dependencies.

**Fix:** Simply retry the command. It is non-deterministic — a failed attempt can succeed on
the very next try with no other change:

```powershell
uvx --extra-index-url http://localhost:8080/simple/ --from rag-mcp-new-pip-mcp rag-mcp-new-pip-mcp serve --no-reindex
```

If it fails repeatedly (3+ consecutive attempts), add a real-time protection exclusion for
`%LOCALAPPDATA%\uv` and `%TEMP%` in Windows Security, or run `uvx` once manually from an
elevated terminal to "warm" the cache before configuring Kiro's `mcp.json` — subsequent
Kiro-initiated launches reuse the cache and don't hit this path again.

## Agent hook for single-project reindex "does nothing"

**Symptom:** The `askAgent`-based hook that's supposed to ask which project to reindex appears
to stop after listing projects — it never prompts for a choice or runs the reindex command.

**Cause:** An `askAgent` hook triggers an agent turn but does not itself support an interactive
prompt-and-wait cycle. If the agent turn executes the first tool call (e.g. `list_projects()`)
and then ends without further instruction, the hook appears to "do nothing" from the user's
perspective — this is expected behavior for a non-interactive automated turn, not a hook bug
per se, but can also happen if Docker/the MCP server isn't running and the tool call itself
fails silently.

**Verify Docker is running first:**

```bash
docker ps --filter "ancestor=rag-mcp-new-pip:latest"
```

If nothing is listed, the MCP server container isn't up — start it before triggering the hook.
