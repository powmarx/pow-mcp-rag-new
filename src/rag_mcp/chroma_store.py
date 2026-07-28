"""
ChromaDB storage layer.

Manages connections to ChromaDB and provides operations for storing,
querying, and managing document chunks and their embeddings.
"""

import sys
from pathlib import Path

import chromadb

from rag_mcp.config_loader import StorageConfig


class ChromaStore:
    """Manages ChromaDB persistent storage and collection operations."""

    def __init__(self, storage_config: StorageConfig):
        self.config = storage_config
        self.client: chromadb.PersistentClient | None = None

    def connect(self) -> None:
        """Connect to ChromaDB (local persistent or remote)."""
        if self.config.mode == "remote" and self.config.url:
            # Phase 2: remote connection
            self.client = chromadb.HttpClient(host=self.config.url)
        else:
            storage_path = Path(self.config.path)
            storage_path.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(storage_path))
        print("[startup] ChromaDB connected", file=sys.stderr)

    def get_or_create_collection(self, project_name: str, description: str = ""):
        """Get or create a collection for a project."""
        collection_name = self._collection_name(project_name)
        return self.client.get_or_create_collection(
            name=collection_name,
            metadata={"description": description},
        )

    def get_collection(self, project_name: str):
        """Get an existing collection. Returns None if not found."""
        collection_name = self._collection_name(project_name)
        try:
            return self.client.get_collection(name=collection_name)
        except (ValueError, Exception):
            return None

    def delete_collection(self, project_name: str) -> None:
        """Delete a project's collection (for --reset)."""
        collection_name = self._collection_name(project_name)
        try:
            self.client.delete_collection(collection_name)
        except (ValueError, Exception):
            pass

    def list_collections(self) -> list:
        """List all collections with the configured prefix."""
        all_cols = self.client.list_collections()
        prefix = self.config.collection_prefix
        return [c for c in all_cols if c.name.startswith(prefix)]

    def get_existing_hash(self, collection, file_path: str) -> str | None:
        """Get the stored hash for a file, or None if not indexed."""
        results = collection.get(
            where={"file_path": file_path},
            include=["metadatas"],
        )
        if results["ids"] and results["metadatas"]:
            return results["metadatas"][0].get("file_hash")
        return None

    def delete_file_chunks(self, collection, file_path: str) -> None:
        """Delete all chunks for a specific file."""
        results = collection.get(where={"file_path": file_path})
        if results["ids"]:
            collection.delete(ids=results["ids"])

    def upsert_chunks(
        self,
        collection,
        file_path: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata_base: dict,
    ) -> None:
        """Store chunks with embeddings and metadata. Handles batch size limits."""
        ids = [f"{file_path}::chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {**metadata_base, "chunk_index": i, "total_chunks": len(chunks)}
            for i in range(len(chunks))
        ]

        # ChromaDB has a max batch size (typically 5461). Batch in groups.
        batch_size = 5000
        for start in range(0, len(ids), batch_size):
            end = start + batch_size
            collection.upsert(
                ids=ids[start:end],
                embeddings=embeddings[start:end],
                documents=chunks[start:end],
                metadatas=metadatas[start:end],
            )

    def get_all_indexed_files(self, collection) -> set[str]:
        """Get all unique file_path values in a collection.

        Filters out offset_tracker records (used for incremental log indexing)
        so they don't appear as real indexed files.
        """
        results = collection.get(include=["metadatas"])
        files = set()
        if results["metadatas"]:
            for meta in results["metadatas"]:
                # Skip offset_tracker records — they are internal bookkeeping
                if meta.get("record_type") == "offset_tracker":
                    continue
                fp = meta.get("file_path")
                if fp:
                    files.add(fp)
        return files

    def _collection_name(self, project_name: str) -> str:
        """Build collection name from prefix and project name."""
        return f"{self.config.collection_prefix}_{project_name}"
