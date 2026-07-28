"""RAG MCP Server - core library modules."""

# All imports are lazy to avoid loading heavy dependencies (chromadb, torch,
# sentence-transformers) when only CLI path resolution is needed (e.g. 'rag-mcp-new-pip-mcp config').
# Import submodules directly when needed:
#   from rag_mcp.chroma_store import ChromaStore
#   from rag_mcp.embedding_generator import EmbeddingGenerator

__all__ = [
    "AppConfig",
    "ChunkingConfig",
    "ChromaStore",
    "Chunker",
    "ConfigLoader",
    "EmbeddingConfig",
    "FileContent",
    "FileReader",
    "PDFExtractor",
    "ProjectAutoDetector",
    "ProjectConfig",
    "SourcePattern",
    "StorageConfig",
]
