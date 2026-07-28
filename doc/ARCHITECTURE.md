# Architecture

## How It Works

1. **Indexing**: Reads files matching configured patterns, splits them into overlapping chunks,
   generates vector embeddings using sentence-transformers, and stores everything in ChromaDB
   with metadata (file path, type, project). Runs as a one-off Docker container (`indexer.py`)
   or via `rag-mcp index` in pip mode.

2. **Log-specific pipeline**: For sources with `type: log`, a dedicated pipeline applies:
   pattern-based parsing → line filtering → content transformation → event grouping →
   embedding. All behavior is driven by YAML configuration — no code changes needed for
   new log formats. See [LOG_INDEXING_GUIDE.md](LOG_INDEXING_GUIDE.md) for details.

3. **Serving**: Runs as an MCP server over stdio (or Streamable HTTP). When Kiro sends a
   search query, it encodes the query into a vector, finds the most similar chunks in
   ChromaDB, and (when reranking is enabled) reorders the top candidates with a cross-encoder
   before returning them with context. See [Retrieval Model](#retrieval-model) below.

4. **Change detection**: The indexer tracks file hashes. Re-running it only re-indexes files
   that have changed since the last run.

5. **Incremental log indexing**: Log files are indexed incrementally using byte-offset tracking.
   Only new content appended since the last index run is processed. If a file is truncated
   (rotated), it is re-indexed from the beginning.

6. **PDF support**: See [PDF Handling](#pdf-handling) below. Conversion always targets a
   writable cache so read-only source mounts are never modified.

7. **Lazy model loading**: The embedding model is NOT loaded on startup (to avoid MCP client
   timeouts). It loads on first search query — this keeps startup under 1 second.

8. **Streaming log indexing**: Large log files (100s of MB) are processed in 10k-line streaming
   batches. Each batch goes through the full pipeline (parse → filter → transform → group →
   embed → store) independently. Progress is reported as a percentage. Interrupting preserves
   already-stored chunks — re-running is idempotent (upsert with deterministic IDs).

9. **On-demand time-window indexing**: The `index_log_file` tool accepts `time_from`/`time_to`
   parameters to index only events within a specific time range. This makes indexing a 1-hour
   window of a 576 MB file take seconds instead of 15+ minutes.

## PDF Handling

PDFs are **always converted to Markdown** before indexing (never indexed as raw bytes):

1. Uses an up-to-date `.md` sibling if one exists.
2. Otherwise converts the PDF into a writable cache (`/app/data/pdf_cache` in Docker, or the
   XDG data dir in pip mode). This works because source mounts can be read-only — no `.md`
   files are written into your repos.
3. Falls back to direct text extraction only for image-only / unconvertible PDFs.

Search results report the logical `.pdf` path (not the cache path), and a PDF is skipped if a
fresh `.md` sibling is already being indexed, so content is never duplicated.

## Embedding Model

Default: `BAAI/bge-small-en-v1.5` (384-dim). Swapped from the original `all-MiniLM-L6-v2`
default (also 384-dim) because it scores measurably higher on retrieval-focused benchmarks
(MTEB retrieval), at the same footprint and dimension — no schema change required. BGE models
benefit from a query-side instruction prefix for best retrieval quality; this is configured via
`embedding.query_instruction` (applied only to queries, never to indexed documents — see
`embedding_generator.py`'s `encode_query()`).

**Changing the model requires a full reindex** — different models produce different vector
distributions, so mixing embeddings from two models in one collection corrupts search results.
Change `embedding.model` in `config.yaml`, rebuild the Docker image so the new model is baked
into the image cache, then run `indexer.py --reset` (or `rag-mcp index --reset` in pip
mode) against the persistent data volume.

## Retrieval Model

Two-stage retrieval: query → embedding → cosine similarity search in ChromaDB retrieves
`top_k * overfetch_factor` (capped at 100) candidates → when `reranker.enabled` (default true),
a cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2` by default) scores each candidate
jointly against the query and re-sorts them → results are truncated to `top_k`. This is a
pure query-time reordering step — it requires no reindex and can be toggled via the `reranker`
section of `config.yaml`. If the reranker model fails to load or raises during scoring, the
server logs the failure and falls back to the bi-encoder-ordered results rather than failing
the search request. There is no hybrid vector+keyword search and no diversity re-ranking (MMR).
`search_hex_pattern`, `find_function`, and `find_variable` use exact text matching
(`where_document: $contains`) rather than vector search, since those queries benefit from
precision over semantic fuzziness.

## File Structure

```
pow-mcp-rag-new/
├── server.py              # MCP server entry point (launched by Docker via mcp.json)
├── indexer.py             # Indexer CLI (run via Docker — see DOCKER_GUIDE.md)
├── start-http.ps1         # PowerShell helper: start HTTP server with auto port-probing
├── setup-docker.bat       # Windows: build image + discover + index + wire up mcp.json
├── setup-docker.sh        # Linux/macOS equivalent of setup-docker.bat
├── setup-pypi.bat         # Windows: build wheel + local PyPI index + wire up mcp.json/.vscode via uvx
├── setup.bat              # Windows: native (non-Docker) local install (legacy)
├── setup.sh               # Linux/macOS equivalent of setup.bat
├── Dockerfile             # Multi-stage image: builder (deps+model) + slim runtime
├── .dockerignore          # Build-context excludes (data/, .git, tests)
├── .gitattributes         # Forces LF for *.sh/Dockerfile, CRLF for *.bat/*.ps1
├── docker-compose.yml     # Compose services: indexer + server + server-http
├── docker/
│   └── entrypoint.sh      # Seeds config.yaml from template into volume on first run
├── restart_wmi.bat        # Fix for WMI blocking SQLite/ChromaDB on Windows (run as admin)
├── README.md              # Entry point / quick start
├── .gitignore             # Excludes data/, config.yaml
├── .vscode/
│   └── mcp.json           # MCP config for VS 2026 / VS Code (uses ${workspaceFolder})
├── pyproject.toml         # Phase 2: pip package definition (rag-mcp); package-data bundles src/rag_mcp/data/*
├── MANIFEST.in            # Phase 2: includes config/*.yaml, *.json in the sdist (source distribution)
├── packages/              # Local PyPI index storage for pypiserver (gitignored) — setup-pypi.bat output
├── requirements.txt       # Runtime dependencies (Docker + pip installs)
├── requirements-dev.txt   # Adds pytest, pytest-asyncio, hypothesis
├── pytest.ini             # Pytest configuration
├── config/
│   ├── config.template.yaml           # Starting config (committed; seeded on first run)
│   ├── config.docker.example.yaml     # Reference config showing container paths
│   ├── detection_rules.json           # Auto-detection rules for --add-project
│   └── server_info.json               # MCP server metadata (name is the mcp.json key)
├── doc/
│   ├── DOCKER_GUIDE.md                 # Full Docker setup and CLI reference
│   ├── PIP_INSTALL_GUIDE.md            # Full pip install guide
│   ├── ARCHITECTURE.md                 # This file
│   ├── TROUBLESHOOTING.md              # Common issues and fixes
│   ├── TOOLS_GUIDE.md                  # MCP tool reference with examples
│   ├── LOG_INDEXING_GUIDE.md           # Log indexing usage guide
│   └── LOG_PATTERN_CONFIGURATION.md    # How to write log pattern configs for new formats
├── scripts/
│   ├── setup_discover.py  # Lists root folders under PROJECTS_ROOT, builds project config
│   ├── setup_mcp_config.py # Writes/merges the MCP server entry into mcp.json / .vscode/mcp.json
│   │                        # (native venv, --docker, and --uvx modes)
│   └── sync_package_data.py # Copies config/*.yaml|json into src/rag_mcp/data/ before building
│                              # the wheel, so pip/uvx installs get the real template (not a stub)
├── src/
│   └── rag_mcp/           # Core library (installable as rag-mcp)
│       ├── cli.py                 # Phase 2 CLI: 'rag-mcp serve|index|config'
│       ├── paths.py                # XDG config/data path resolution (pip install mode)
│       ├── data/                   # Config templates bundled INSIDE the wheel (package-data).
│       │   │                        # Kept in sync with config/ via sync_package_data.py — this
│       │   │                        # is what makes config seeding work for pip/uvx installs,
│       │   │                        # since MANIFEST.in alone only affects the sdist, not the wheel.
│       │   ├── config.template.yaml
│       │   ├── server_info.json
│       │   └── detection_rules.json
│       ├── _server.py              # Importable wrapper delegating to server.py (or inline fallback
│       │                            # with its own --list-tools support if server.py isn't found)
│       ├── _indexer.py             # Importable wrapper delegating to indexer.py
│       ├── config_loader.py       # Configuration parsing and validation
│       ├── file_reader.py         # File reading (encoding, binary, PDF)
│       ├── pdf_extractor.py       # PDF text extraction (fallback for image-only PDFs)
│       ├── pdf_converter.py       # PDF to Markdown conversion
│       ├── chunker.py             # Text chunking with configurable separators
│       ├── embedding_generator.py # SentenceTransformer wrapper
│       ├── chroma_store.py        # ChromaDB connection and operations
│       ├── auto_detector.py       # Project structure auto-detection (add_project tool)
│       ├── source_scanner.py      # Builds recursive source patterns from index_extensions
│       ├── indexing_pipeline.py   # Orchestrates the indexing flow
│       ├── tools/                 # MCP tool implementations (21 tools total)
│       │   ├── search.py              # search_docs, search_specs, search_code, etc.
│       │   ├── management.py          # add_project, add_pattern, remove_project, etc.
│       │   ├── documents.py           # get_document, list_files, get_project_summary
│       │   ├── logs.py                # index_log_file, search_logs, cancel_indexing
│       │   └── helpers.py             # Shared helpers (FILE_TYPE_MAP, etc.)
│       └── log/                   # Structured log indexing
│           ├── config_models.py       # Log-specific config dataclasses
│           ├── severity.py            # Severity normalization
│           ├── line_filter.py         # Include/exclude line filtering
│           ├── content_transform.py   # Content transformation (extract/replace/strip/collapse)
│           ├── log_parser.py          # Pattern-based log line parser
│           ├── event_grouper.py       # Event deduplication and grouping
│           └── log_indexer.py         # Log indexing orchestration + incremental offsets
├── tests/                 # Unit/integration tests (pytest)
├── .kiro/
│   └── specs/             # Feature specifications
└── data/                  # Local ChromaDB storage (gitignored — use the Docker volume in production)
```
