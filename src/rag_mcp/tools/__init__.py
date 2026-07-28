"""
MCP tool implementations for the RAG server.

Tools are organized by function:
- search.py:     Semantic search, hex pattern, variable/function lookup
- documents.py:  Document retrieval, project listing, summaries
- management.py: Project/file/folder addition and removal
- logs.py:       Log search, indexing, cancellation
"""

from dataclasses import dataclass, field
from typing import Any

from rag_mcp.chroma_store import ChromaStore
from rag_mcp.config_loader import AppConfig, ConfigLoader
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.reranker import Reranker


@dataclass
class ToolContext:
    """Shared context passed to all tool modules at registration time."""

    config: AppConfig
    loader: ConfigLoader
    store: ChromaStore
    embedding_gen: EmbeddingGenerator
    ensure_model_loaded: Any  # Callable[[], None]
    reindex_in_progress: Any  # Callable[[], bool]
    indexing_cancelled: Any   # Callable[[], bool]
    set_indexing_cancelled: Any  # Callable[[bool], None]
    reranker: Reranker | None = None
    ensure_reranker_loaded: Any = None  # Callable[[], None]


def register_all_tools(mcp, ctx: ToolContext) -> None:
    """Register all MCP tools from submodules."""
    from rag_mcp.tools import search, documents, management, logs

    search.register(mcp, ctx)
    documents.register(mcp, ctx)
    management.register(mcp, ctx)
    logs.register(mcp, ctx)
