"""
Regression tests for rag_mcp._server._run_inline().

_run_inline() is the startup path used by the installed (pip/uvx) package
when server.py is not found on disk (i.e. every real installed-package run,
since pyproject.toml only bundles src/). server.py's own startup comments
document why the embedding model must be loaded *before* mcp.run() starts
the event loop:

    # This avoids the httpx/anyio deadlock (model loads before event loop)
    # while also avoiding the connection timeout (mcp.run() isn't blocked
    # by loading).

_run_inline() previously skipped this preload step entirely, leaving
ensure_model_loaded fully lazy and unsynchronized. That meant the first real
tool call loaded the model from inside an anyio worker thread, after the
event loop was already running — reproducing exactly the deadlock server.py
was written to avoid, and with no lock, two concurrent first calls could
race to construct SentenceTransformer twice.

These tests verify the fix: the model is loaded synchronously before
mcp.run() is invoked, and ensure_model_loaded is safe to call concurrently
without loading twice.
"""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "src"))

import rag_mcp._server as server_module
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.chroma_store import ChromaStore


@pytest.fixture
def inline_env(tmp_path, monkeypatch):
    """Point the server at a throwaway config/data dir and force the
    _run_inline() fallback path (as if server.py were not found on disk).

    _run_inline() calls register_all_tools(), which overwrites the
    module-level ``_ctx`` singleton in every rag_mcp.tools.* submodule
    (search.py, documents.py, management.py, logs.py). Those singletons are
    shared with the real server fixture used by other test modules
    (tests/mcp_tools/*), so we snapshot and restore them here to avoid
    leaking this test's fake ToolContext into unrelated tests.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "embedding:\n  model: BAAI/bge-small-en-v1.5\n"
        "reranker:\n  enabled: false\n"
        "storage:\n  path: '" + str(tmp_path / "data").replace("\\", "/") + "'\n"
        "  collection_prefix: rag\n  mode: local\n"
        "chunking:\n  chunk_size: 1000\n  chunk_overlap: 200\n"
        "projects: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(sys, "argv", ["rag-mcp"])

    import rag_mcp.tools.search as _search_mod
    import rag_mcp.tools.documents as _documents_mod
    import rag_mcp.tools.management as _management_mod
    import rag_mcp.tools.logs as _logs_mod

    for mod in (_search_mod, _documents_mod, _management_mod, _logs_mod):
        monkeypatch.setattr(mod, "_ctx", mod._ctx)

    return config_path


def test_run_inline_loads_model_before_mcp_run(inline_env, monkeypatch):
    """The embedding model must finish loading before mcp.run() is called —
    never lazily from inside a tool call on the event loop's worker thread."""
    call_order = []

    def fake_load(self):
        call_order.append("model_load")
        self.model = object()  # sentinel: "loaded"

    def fake_connect(self):
        call_order.append("chroma_connect")
        self.client = object()

    def fake_mcp_run(self, *args, **kwargs):
        call_order.append("mcp_run")

    monkeypatch.setattr(EmbeddingGenerator, "load", fake_load)
    monkeypatch.setattr(ChromaStore, "connect", fake_connect)
    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run", fake_mcp_run)

    server_module._run_inline()

    assert "model_load" in call_order, "embedding model was never loaded"
    assert "mcp_run" in call_order, "mcp.run() was never called"
    assert call_order.index("model_load") < call_order.index("mcp_run"), (
        f"model must load before mcp.run() starts the event loop, got order: {call_order}"
    )


def test_run_inline_does_not_reload_model_if_already_loaded(inline_env, monkeypatch):
    """ensure_model_loaded should be a no-op once the model is set (avoids
    reloading on every tool call after the initial preload)."""
    load_calls = []

    def fake_load(self):
        load_calls.append(1)
        self.model = object()

    monkeypatch.setattr(EmbeddingGenerator, "load", fake_load)
    monkeypatch.setattr(ChromaStore, "connect", lambda self: setattr(self, "client", object()))
    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run", lambda self, *a, **k: None)

    captured_ctx = {}
    import rag_mcp.tools as tools_module
    real_register = tools_module.register_all_tools

    def capture_ctx(mcp, ctx):
        captured_ctx["ctx"] = ctx
        return real_register(mcp, ctx)

    monkeypatch.setattr(tools_module, "register_all_tools", capture_ctx)

    server_module._run_inline()

    assert len(load_calls) == 1, "model should have loaded exactly once during startup"

    # Calling ensure_model_loaded again (as every tool call does) must not
    # trigger a second load.
    captured_ctx["ctx"].ensure_model_loaded()
    assert len(load_calls) == 1, "ensure_model_loaded reloaded an already-loaded model"


def test_ensure_model_loaded_is_race_safe(inline_env, monkeypatch):
    """Two concurrent first-time calls to ensure_model_loaded must not both
    construct the model — the lock must serialize them."""
    load_calls = []
    load_lock_probe = threading.Lock()

    def slow_load(self):
        # Simulate the real ~seconds-long SentenceTransformer construction,
        # giving a second thread a window to race in if unsynchronized.
        with load_lock_probe:
            load_calls.append(1)
        time.sleep(0.2)
        self.model = object()

    monkeypatch.setattr(EmbeddingGenerator, "load", slow_load)
    monkeypatch.setattr(ChromaStore, "connect", lambda self: setattr(self, "client", object()))
    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run", lambda self, *a, **k: None)

    captured_ctx = {}
    import rag_mcp.tools as tools_module
    real_register = tools_module.register_all_tools

    def capture_ctx(mcp, ctx):
        captured_ctx["ctx"] = ctx
        return real_register(mcp, ctx)

    monkeypatch.setattr(tools_module, "register_all_tools", capture_ctx)

    server_module._run_inline()
    # Startup already loaded it once; reset to simulate a fresh unloaded
    # model so we can exercise the concurrent-call path in isolation.
    captured_ctx["ctx"].embedding_gen.model = None
    load_calls.clear()

    threads = [
        threading.Thread(target=captured_ctx["ctx"].ensure_model_loaded)
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(load_calls) == 1, (
        f"expected exactly 1 model load across concurrent callers, got {len(load_calls)} "
        "(unsynchronized ensure_model_loaded can double-construct the model)"
    )
