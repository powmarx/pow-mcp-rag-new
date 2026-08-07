"""
RAG MCP Server for project documentation.

Exposes semantic search tools over indexed project documentation via the
Model Context Protocol (MCP). Designed to be used with Kiro or any MCP client.

Usage:
    python server.py                    # Start the MCP server (stdio transport)
    python server.py --list-tools       # Show available tools
    python server.py --no-reindex       # Start without auto-reindex (faster startup)
"""

import json
import os
import sys
import time
import threading
import warnings
from pathlib import Path

# Force stderr to be line-buffered so background thread logs appear immediately
# in MCP clients (Kiro, VS Code) that capture stderr via pipe.
sys.stderr.reconfigure(line_buffering=True)

# Add src/ to path so rag_mcp package is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

# pydantic-settings may emit an IncompleteFieldDefinitionWarning for FastMCP's
# internal "lifespan" field forward ref on some dependency combinations. This
# is upstream and does not affect runtime behavior for this server.
try:
    from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning
except ImportError:
    IncompleteFieldDefinitionWarning = None

if IncompleteFieldDefinitionWarning is not None:
    warnings.filterwarnings(
        "ignore",
        message=r".*Field 'lifespan' has an incomplete definition.*",
        category=IncompleteFieldDefinitionWarning,
    )

from mcp.server.fastmcp import FastMCP

from rag_mcp.chroma_store import ChromaStore
from rag_mcp.config_loader import ConfigLoader
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.reranker import Reranker
from rag_mcp.tools import ToolContext, register_all_tools

# Resolve paths relative to this script. RAG_CONFIG_PATH env overrides the
# config location (Docker points this at the data volume: /app/data/config.yaml).
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("RAG_CONFIG_PATH") or (SCRIPT_DIR / "config" / "config.yaml"))

# ---------------------------------------------------------------------------
# Shared mutable state (accessed via ToolContext callables)
# ---------------------------------------------------------------------------

_reindex_in_progress = False
_indexing_cancelled = False

# Lock to prevent concurrent embedding model access (model is not thread-safe)
_embedding_lock = threading.Lock()
_reranker_lock = threading.Lock()


def _set_indexing_cancelled(value: bool):
    global _indexing_cancelled
    _indexing_cancelled = value


def _ensure_model_loaded():
    """Wait for the background model load to complete. Blocks until model is ready."""
    if embedding_gen.model is not None:
        return
    # Wait for background model loading thread if it's running
    if '_model_load_event' in globals() and not _model_load_event.is_set():
        _model_load_event.wait(timeout=30)
    with _embedding_lock:
        if embedding_gen.model is None:
            print("[reindex] Loading embedding model...", file=sys.stderr)
            load_start = time.time()
            embedding_gen.load()
            load_ms = (time.time() - load_start) * 1000
            print(f"[reindex] Embedding model loaded in {load_ms:.0f}ms", file=sys.stderr)


def _ensure_reranker_loaded():
    """Lazily load the cross-encoder reranker model on first search. No-op if disabled."""
    if reranker is None or reranker.model is not None:
        return
    with _reranker_lock:
        if reranker.model is None:
            print("[search] Loading reranker model...", file=sys.stderr)
            load_start = time.time()
            reranker.load()
            load_ms = (time.time() - load_start) * 1000
            print(f"[search] Reranker model loaded in {load_ms:.0f}ms", file=sys.stderr)


# ---------------------------------------------------------------------------
# Startup (keep minimal to avoid MCP client timeout)
# ---------------------------------------------------------------------------

startup_start = time.time()

# Load configuration
try:
    loader = ConfigLoader(CONFIG_PATH)
    config = loader.load()
except Exception as e:
    print(f"[fatal] Failed to load config: {e}", file=sys.stderr)
    sys.exit(1)

# Initialize embedding model (lazy - loaded on first use)
embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
print(f"[startup] Embedding model configured: {config.embedding.model} (lazy load)", file=sys.stderr)

# Initialize reranker (lazy - loaded on first search). None if disabled in config.
reranker = Reranker(config.reranker.model) if config.reranker.enabled else None
if reranker:
    print(f"[startup] Reranker configured: {config.reranker.model} (lazy load)", file=sys.stderr)
else:
    print("[startup] Reranker disabled", file=sys.stderr)

# Connect to ChromaDB
try:
    if not Path(config.storage.path).is_absolute():
        config.storage.path = str(SCRIPT_DIR / config.storage.path)
    store = ChromaStore(config.storage)
    store.connect()
except Exception as e:
    print(f"[fatal] Failed to connect to ChromaDB: {e}", file=sys.stderr)
    sys.exit(1)

startup_ms = (time.time() - startup_start) * 1000
print(f"[startup] Core ready in {startup_ms:.0f}ms", file=sys.stderr)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def _log_index_status():
    try:
        collections = store.list_collections()
        if not collections:
            print(
                "[startup] WARNING: No projects indexed yet. "
                "Run 'python indexer.py' to index your projects before searching.",
                file=sys.stderr,
            )
        else:
            total_chunks = sum(
                store.client.get_collection(name=c.name).count() for c in collections
            )
            print(
                f"[startup] {len(collections)} project(s) indexed, {total_chunks} total chunks available",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[startup] Could not check index status: {e}", file=sys.stderr)

threading.Thread(target=_log_index_status, daemon=True).start()


def _background_reindex():
    """Run auto-reindex in background thread so it doesn't block MCP handshake."""
    global _reindex_in_progress, _indexing_cancelled
    _reindex_in_progress = True
    _indexing_cancelled = False
    try:
        from rag_mcp.file_reader import FileReader
        from rag_mcp.chunker import Chunker
        from rag_mcp.indexing_pipeline import IndexingPipeline

        _ensure_model_loaded()

        file_reader = FileReader()
        chunker = Chunker(config.chunking)

        class SilentConsole:
            def print(self, *args, **kwargs):
                pass

        pipeline = IndexingPipeline(
            config=config,
            file_reader=file_reader,
            chunker=chunker,
            embedding_gen=embedding_gen,
            store=store,
            console=SilentConsole(),
        )

        total_new_chunks = 0
        reindex_start = time.time()
        eligible_projects = [
            p for p in config.projects
            if p.auto_reindex and not p.removed and Path(p.base_path).exists()
        ]
        skipped = [p for p in config.projects if not p.auto_reindex or p.removed]

        print(
            f"[reindex] {len(eligible_projects)} project(s) to index"
            + (f", {len(skipped)} skipped (auto_reindex=false or removed)" if skipped else ""),
            file=sys.stderr,
        )

        for proj_idx, project in enumerate(eligible_projects, 1):
            if _indexing_cancelled:
                print(f"[reindex] Cancelled after {proj_idx-1}/{len(eligible_projects)} projects", file=sys.stderr)
                break

            project_start = time.time()
            print(f"[reindex] [{proj_idx}/{len(eligible_projects)}] {project.name}...", file=sys.stderr)
            chunks = pipeline.index_project(project, is_cancelled=lambda: _indexing_cancelled)
            project_ms = (time.time() - project_start) * 1000

            if chunks > 0:
                print(f"[reindex] [{proj_idx}/{len(eligible_projects)}] {project.name}: {chunks} chunks ({project_ms:.0f}ms)", file=sys.stderr)
            else:
                print(f"[reindex] [{proj_idx}/{len(eligible_projects)}] {project.name}: up to date ({project_ms:.0f}ms)", file=sys.stderr)
            total_new_chunks += chunks

        reindex_ms = (time.time() - reindex_start) * 1000
        if _indexing_cancelled:
            print(f"[reindex] Stopped (cancelled): {total_new_chunks} chunks in {reindex_ms:.0f}ms", file=sys.stderr)
        elif total_new_chunks > 0:
            print(f"[reindex] Done: {total_new_chunks} chunks in {reindex_ms:.0f}ms", file=sys.stderr)
        else:
            print(f"[reindex] Done: up to date ({reindex_ms:.0f}ms)", file=sys.stderr)

        _reindex_in_progress = False
    except Exception as e:
        _reindex_in_progress = False
        print(f"[reindex] Failed: {e}", file=sys.stderr)


# Launch background reindex unless --no-reindex flag is passed
if "--no-reindex" not in sys.argv:
    # Load embedding model in a background thread BEFORE the event loop starts.
    # The thread starts immediately (while the rest of module-level code executes
    # and mcp.run() initializes). By the time the event loop is active, the model
    # is either already loaded or nearly done. _ensure_model_loaded() blocks until
    # complete.
    # This avoids the httpx/anyio deadlock (model loads before event loop) while
    # also avoiding the connection timeout (mcp.run() isn't blocked by loading).
    _model_load_event = threading.Event()

    def _load_model_thread():
        print("[startup] Loading embedding model (background)...", file=sys.stderr)
        load_start = time.time()
        embedding_gen.load()
        load_ms = (time.time() - load_start) * 1000
        print(f"[startup] Embedding model loaded in {load_ms:.0f}ms", file=sys.stderr)
        _model_load_event.set()

    model_thread = threading.Thread(target=_load_model_thread, daemon=True)
    model_thread.start()

    def _wait_for_model_then_reindex():
        # Wait for model loading to complete before reindexing
        _model_load_event.wait()
        _background_reindex()

    reindex_thread = threading.Thread(target=_wait_for_model_then_reindex, daemon=True)
    reindex_thread.start()
    print("[startup] Background reindex scheduled (waiting for model)", file=sys.stderr)
else:
    print("[startup] Auto-reindex disabled (--no-reindex)", file=sys.stderr)

# Pre-load embedding model before the event loop starts (--no-reindex mode).
# In this mode there's no background thread, so we must load synchronously.
# The reranker stays lazy (loaded on first search) even in this mode since it
# isn't needed for the reindex path and keeps startup fast.
if "--no-reindex" in sys.argv:
    print("[startup] Loading embedding model (required before event loop)...", file=sys.stderr)
    embedding_gen.load()
    print(f"[startup] Model ready", file=sys.stderr)


# ---------------------------------------------------------------------------
# Create MCP server and register tools
# ---------------------------------------------------------------------------

_server_info_path = SCRIPT_DIR / "config" / "server_info.json"
with open(_server_info_path, "r", encoding="utf-8") as _f:
    _server_info = json.load(_f)

# For HTTP mode, resolve port before creating FastMCP (host/port are constructor args).
_http_mode = "--http" in sys.argv
_http_port = 8000
_http_host = "0.0.0.0"
_http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")

if _http_mode:
    import socket as _socket

    def _find_free_port(start: int, max_tries: int = 10) -> int:
        for port in range(start, start + max_tries):
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", port))
                    return port
                except OSError:
                    continue
        raise OSError(
            f"No free port found in range {start}–{start + max_tries - 1}. "
            f"Set MCP_HTTP_PORT to an explicit free port."
        )

    env_port = os.environ.get("MCP_HTTP_PORT")
    if env_port:
        _http_port = int(env_port)
        print(f"[startup] HTTP transport on {_http_host}:{_http_port}{_http_path} (MCP_HTTP_PORT)", file=sys.stderr)
    else:
        arg_port = None
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                arg_port = int(sys.argv[idx + 1])
        requested = arg_port or 8000
        _http_port = _find_free_port(requested)
        if _http_port != requested:
            print(f"[startup] Port {requested} in use — using {_http_port} instead", file=sys.stderr)
        print(f"[startup] HTTP transport on {_http_host}:{_http_port}{_http_path}", file=sys.stderr)

# FastMCP constructor: pass host/port for HTTP mode; defaults (127.0.0.1:8000) for stdio.
if _http_mode:
    mcp = FastMCP(_server_info["name"], host=_http_host, port=_http_port, streamable_http_path=_http_path)
else:
    mcp = FastMCP(_server_info["name"])

# Build the shared tool context
ctx = ToolContext(
    config=config,
    loader=loader,
    store=store,
    embedding_gen=embedding_gen,
    ensure_model_loaded=_ensure_model_loaded,
    reindex_in_progress=lambda: _reindex_in_progress,
    indexing_cancelled=lambda: _indexing_cancelled,
    set_indexing_cancelled=_set_indexing_cancelled,
    reranker=reranker,
    ensure_reranker_loaded=_ensure_reranker_loaded,
)

# Register all tools from submodules
register_all_tools(mcp, ctx)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--list-tools" in sys.argv:
        print("Available tools:")
        for tool in sorted(mcp._tool_manager._tools.values(), key=lambda t: t.name):
            params = []
            if hasattr(tool, 'parameters') and tool.parameters:
                schema = tool.parameters.get('properties', {})
                required = tool.parameters.get('required', [])
                for pname in schema:
                    suffix = "" if pname in required else "?"
                    params.append(f"{pname}{suffix}")
            params_str = ", ".join(params)
            print(f"  - {tool.name}({params_str})")
            if tool.description:
                first_line = tool.description.strip().split('\n')[0]
                print(f"      {first_line}")
    elif "--http" in sys.argv:
        # Port and host already resolved above; FastMCP was constructed with them.
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
