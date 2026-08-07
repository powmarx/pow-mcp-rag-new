"""
Importable entry point for the MCP server.

Used by the CLI (rag-mcp serve) so the server can be run
both as a standalone script (python server.py) and as an installed package.

When running as a package, RAG_CONFIG_PATH is already set by cli.py.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path


def _suppress_fastmcp_lifespan_warning() -> None:
    """Suppress known upstream pydantic-settings warning from FastMCP internals."""
    warnings.filterwarnings(
        "ignore",
        message=r".*lifespan.*incomplete definition.*",
        module=r"pydantic_settings\.sources\.utils",
    )
    try:
        from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning
    except ImportError:
        return
    warnings.filterwarnings(
        "ignore",
        message=r".*lifespan.*incomplete definition.*",
        category=IncompleteFieldDefinitionWarning,
    )


def main() -> None:
    """Run the MCP server. Delegates to server.py logic."""
    # Ensure src/ is on the path when running as an installed package
    # (importlib already handles this, but keep for editable installs)
    here = Path(__file__).parent
    src_dir = here.parent
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    # Locate server.py: next to src/ (repo layout) or installed as package data
    candidates = [
        here.parent.parent / "server.py",                  # repo root
        Path(sys.prefix) / "lib" / "rag_mcp" / "server.py",  # installed
    ]

    server_py = next((p for p in candidates if p.exists()), None)

    if server_py:
        # Run server.py as __main__ so its module-level startup code executes
        import runpy
        runpy.run_path(str(server_py), run_name="__main__")
    else:
        # Fallback: import and run inline (for future refactor where server
        # logic moves fully into the package)
        _run_inline()


def _run_inline() -> None:
    """Inline server startup — used when server.py is not found on disk.

    Mirrors server.py's startup sequencing exactly (see that file's
    "Startup" section). In particular, the embedding model MUST be loaded
    before mcp.run() starts the event loop: loading it lazily from inside a
    tool call (via anyio.to_thread.run_sync) is the "httpx/anyio deadlock"
    server.py's comments warn about — the first tool call can hang
    indefinitely instead of just being slow. This bit the installed
    (pip/uvx) package specifically because this fallback path used to skip
    the preload step that only existed in server.py.
    """
    import json
    import time
    import threading

    sys.stderr.reconfigure(line_buffering=True)

    _suppress_fastmcp_lifespan_warning()

    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.config_loader import ConfigLoader
    from rag_mcp.embedding_generator import EmbeddingGenerator
    from rag_mcp.reranker import Reranker
    from rag_mcp.tools import ToolContext, register_all_tools
    from mcp.server.fastmcp import FastMCP

    # Config path already set by cli.py via RAG_CONFIG_PATH
    config_path = Path(os.environ.get("RAG_CONFIG_PATH", ""))
    if not config_path.exists():
        print(f"[fatal] Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    loader = ConfigLoader(config_path)
    config = loader.load()

    # Resolve data path
    from rag_mcp.paths import resolve_data_path
    data_path = resolve_data_path()
    if not Path(config.storage.path).is_absolute():
        config.storage.path = str(data_path)

    store = ChromaStore(config.storage)
    store.connect()

    embedding_gen = EmbeddingGenerator(config.embedding.model, config.embedding.query_instruction)
    reranker = Reranker(config.reranker.model) if config.reranker.enabled else None

    # Lock to prevent concurrent embedding model access/loading (model is not
    # thread-safe, and without this two first-time tool calls can race to
    # both call embedding_gen.load() on separate anyio worker threads).
    _embedding_lock = threading.Lock()
    _reranker_lock = threading.Lock()

    def _ensure_model_loaded_inline():
        if embedding_gen.model is not None:
            return
        with _embedding_lock:
            if embedding_gen.model is None:
                embedding_gen.load()

    def _ensure_reranker_loaded_inline():
        if reranker is None or reranker.model is not None:
            return
        with _reranker_lock:
            if reranker.model is None:
                reranker.load()

    # Pre-load the embedding model before mcp.run() starts the event loop
    # (see docstring above). No auto-reindex path exists in this fallback,
    # so this mirrors server.py's synchronous --no-reindex preload branch
    # unconditionally.
    print("[startup] Loading embedding model (required before event loop)...", file=sys.stderr)
    _load_start = time.time()
    _ensure_model_loaded_inline()
    print(f"[startup] Model ready ({(time.time() - _load_start) * 1000:.0f}ms)", file=sys.stderr)

    # Server info: prefer bundled package data (installed wheel/sdist), fall
    # back to the repo's config/ folder (editable install / repo checkout).
    here = Path(__file__).parent
    info_candidates = [
        here / "data" / "server_info.json",
        here.parent.parent / "config" / "server_info.json",
    ]
    server_info = {"name": "rag-mcp", "version": "1.0.0"}
    for p in info_candidates:
        if p.exists():
            server_info = json.loads(p.read_text(encoding="utf-8"))
            break

    http_mode = "--http" in sys.argv
    http_path = os.environ.get("MCP_HTTP_PATH", "/mcp")

    if http_mode:
        import socket
        port = int(os.environ.get("MCP_HTTP_PORT", "8000"))
        mcp = FastMCP(server_info["name"], host="0.0.0.0", port=port, streamable_http_path=http_path)
    else:
        mcp = FastMCP(server_info["name"])

    _reindex_in_progress = False
    _indexing_cancelled = False

    ctx = ToolContext(
        config=config,
        loader=loader,
        store=store,
        embedding_gen=embedding_gen,
        ensure_model_loaded=_ensure_model_loaded_inline,
        reindex_in_progress=lambda: _reindex_in_progress,
        indexing_cancelled=lambda: _indexing_cancelled,
        set_indexing_cancelled=lambda v: None,
        reranker=reranker,
        ensure_reranker_loaded=_ensure_reranker_loaded_inline,
    )
    register_all_tools(mcp, ctx)

    if "--list-tools" in sys.argv:
        print("Available tools:")
        for tool in sorted(mcp._tool_manager._tools.values(), key=lambda t: t.name):
            params = []
            if hasattr(tool, "parameters") and tool.parameters:
                schema = tool.parameters.get("properties", {})
                required = tool.parameters.get("required", [])
                for pname in schema:
                    suffix = "" if pname in required else "?"
                    params.append(f"{pname}{suffix}")
            params_str = ", ".join(params)
            print(f"  - {tool.name}({params_str})")
            if tool.description:
                first_line = tool.description.strip().split("\n")[0]
                print(f"      {first_line}")
        return

    if http_mode:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
