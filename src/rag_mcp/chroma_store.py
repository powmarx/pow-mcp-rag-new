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
            metadata={
                "description": description,
                "hnsw:space": "cosine",
            },        
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

    def backfill_log_epochs(self, collection) -> int:
        """Add numeric epoch fields to log chunks indexed before they existed.

        Log chunks written by older versions only carry ISO string timestamps
        (``timestamp_range_start`` / ``timestamp_range_end``), which ChromaDB
        cannot range-filter. This lazily derives ``ts_start_epoch`` /
        ``ts_end_epoch`` for those records in place, so no re-index is needed.

        Records whose ISO timestamps don't parse are left untouched (they
        have no usable time and are excluded from time-range queries).

        Returns the number of records updated.
        """
        from rag_mcp.log.timestamps import TS_START_EPOCH_KEY, epoch_fields

        all_log = collection.get(where={"file_type": "log"}, include=[])
        all_ids = all_log["ids"] if all_log and all_log["ids"] else []
        if not all_ids:
            return 0

        # Records that already carry an epoch field need no work.
        with_epoch = collection.get(
            where={"$and": [{"file_type": "log"}, {TS_START_EPOCH_KEY: {"$gte": 0}}]},
            include=[],
        )
        done = set(with_epoch["ids"]) if with_epoch and with_epoch["ids"] else set()
        missing = [i for i in all_ids if i not in done]
        if not missing:
            return 0

        updated = 0
        batch_size = 5000
        for start in range(0, len(missing), batch_size):
            batch_ids = missing[start : start + batch_size]
            rows = collection.get(ids=batch_ids, include=["metadatas"])
            upd_ids: list[str] = []
            upd_meta: list[dict] = []
            for rid, meta in zip(rows["ids"], rows["metadatas"]):
                meta = meta or {}
                fields = epoch_fields(
                    meta.get("timestamp_range_start"),
                    meta.get("timestamp_range_end"),
                )
                if not fields:
                    continue
                upd_ids.append(rid)
                # collection.update merges metadata keys; existing ones are kept.
                upd_meta.append(fields)
            if upd_ids:
                collection.update(ids=upd_ids, metadatas=upd_meta)
                updated += len(upd_ids)

        if updated:
            print(
                f"[search_logs] backfilled epoch timestamps on {updated} log chunks "
                f"in {collection.name}",
                file=sys.stderr,
            )
        return updated

    def _collection_name(self, project_name: str) -> str:
        """Build collection name from prefix and project name."""
        return f"{self.config.collection_prefix}_{project_name}"
