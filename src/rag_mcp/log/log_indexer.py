"""
Log indexer module for structured log indexing.

Delegates parsing to LogPipeline and handles the storage-specific concerns:
embed → store in ChromaDB, byte-offset tracking for incremental indexing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.embedding_generator import EmbeddingGenerator

from rag_mcp.log.parsing.config_models import LogPatternConfig, LogSettings
from rag_mcp.log.parsing.content_transform import ContentTransform
from rag_mcp.log.parsing.event_grouper import EventGroup, EventGrouper
from rag_mcp.log.parsing.log_parser import LogParser
from rag_mcp.log.parsing.log_pipeline import LogPipeline


class LogIndexer:
    """Handles storage-layer concerns for log indexing: embed → store → offset tracking.

    Delegates all parsing logic (boundary detection, pattern matching, transform,
    grouping) to LogPipeline and focuses on:
    - Generating embeddings for group texts
    - Storing chunks in ChromaDB with extended metadata
    - Tracking byte offsets for incremental re-indexing
    """

    # Embedding dimension for MiniLM-L6-v2 (used for zero-vector offset records)
    _DEFAULT_EMBEDDING_DIM = 384

    def __init__(
        self,
        log_parser: LogParser,
        content_transform: ContentTransform,
        event_grouper: EventGrouper,
        embedding_gen: "EmbeddingGenerator",
        store: "ChromaStore",
        settings: LogSettings | None = None,
        patterns: list[LogPatternConfig] | None = None,
    ) -> None:
        """Initialize LogIndexer with all pipeline components.

        Args:
            log_parser: Parser for extracting structured events from raw log text.
            content_transform: Transformer for cleaning event text before embedding.
            event_grouper: Grouper for combining related events into logical units.
            embedding_gen: Generator for producing vector embeddings from text.
            store: ChromaDB storage layer for persisting chunks.
            settings: Optional LogSettings for DMP fallback filter access.
            patterns: Optional list of log patterns for rebuilding parser with fallback.
        """
        self._pipeline = LogPipeline(
            log_parser=log_parser,
            content_transform=content_transform,
            event_grouper=event_grouper,
            settings=settings,
            patterns=patterns,
        )
        self._parser = log_parser
        self._transform = content_transform
        self._grouper = event_grouper
        self._embedding_gen = embedding_gen
        self._store = store
        self._settings = settings
        self._patterns = patterns

    def _get_stored_offset(self, collection: "Collection", file_path: str) -> int:
        """Retrieve the stored byte offset for a file, or 0 if none exists.

        The offset is stored as a special metadata record in ChromaDB with
        ID format: `__offset__<file_path>`.

        Args:
            collection: ChromaDB collection to query.
            file_path: The file path to look up.

        Returns:
            The stored byte offset as an integer, or 0 if no record found.
        """
        record_id = f"__offset__{file_path}"
        try:
            result = collection.get(ids=[record_id], include=["metadatas"])
            if result["ids"] and result["metadatas"]:
                metadata = result["metadatas"][0]
                return int(metadata.get("byte_offset", 0))
        except Exception:
            pass
        return 0

    def _store_offset(self, collection: "Collection", file_path: str, offset: int) -> None:
        """Store or update the byte offset for a file in ChromaDB.

        The offset is stored as a non-searchable record with:
        - Empty document text
        - Zero-vector embedding (won't appear in similarity searches)
        - Metadata with record_type="offset_tracker"

        Args:
            collection: ChromaDB collection to store the offset in.
            file_path: The file path this offset belongs to.
            offset: The byte offset value to store.
        """
        record_id = f"__offset__{file_path}"
        # Determine embedding dimension from the model
        dim = self._get_embedding_dimension()
        zero_vector = [0.0] * dim

        metadata = {
            "file_path": file_path,
            "byte_offset": offset,
            "record_type": "offset_tracker",
            "last_indexed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        collection.upsert(
            ids=[record_id],
            embeddings=[zero_vector],
            documents=[""],
            metadatas=[metadata],
        )

    def _get_embedding_dimension(self) -> int:
        """Get the embedding dimension from the model.

        Attempts to read from the model; falls back to 384 (MiniLM-L6-v2 default).

        Returns:
            Integer embedding dimension.
        """
        # Try to get dimension from the model
        if hasattr(self._embedding_gen, "model") and self._embedding_gen.model is not None:
            dim = self._embedding_gen.model.get_sentence_embedding_dimension()
            if dim:
                return int(dim)
        return self._DEFAULT_EMBEDDING_DIM

    def _find_entry_boundary(self, content: str) -> int:
        """Find the character offset of the first complete log entry in content.

        Delegates to LogPipeline.find_entry_boundary().

        Args:
            content: The raw log content to scan.

        Returns:
            Character offset of the first line that matches a configured pattern.
        """
        return self._pipeline.find_entry_boundary(content)

    def _select_parser(self, content: str) -> LogParser:
        """Select the appropriate parser based on content characteristics.

        Delegates to LogPipeline.select_parser().

        Args:
            content: The log content to analyze.

        Returns:
            LogParser instance — either the standard one or a fallback.
        """
        return self._pipeline.select_parser(content)

    def _build_chunk_metadata(
        self,
        group: EventGroup,
        file_path: str,
        project_name: str,
        source_desc: str,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> dict:
        """Build the full metadata dict for a chunk from an EventGroup.

        Includes both standard chunk metadata fields and log-specific fields.

        Args:
            group: The EventGroup to extract metadata from.
            file_path: Source file path.
            project_name: Project name.
            source_desc: Source description.
            chunk_index: Index of this chunk within the file.
            total_chunks: Total number of chunks for the file.

        Returns:
            Complete metadata dictionary for ChromaDB storage.
        """
        return {
            # Standard fields
            "file_path": file_path,
            "file_type": "log",
            "project": project_name,
            "source_description": source_desc,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            # Log-specific fields
            "event_type": group.event_type or "",
            "severity": group.severity or "",
            "device_id": group.device_id or "",
            "error_code": group.error_code or "",
            "timestamp_range_start": group.timestamp_start or "",
            "timestamp_range_end": group.timestamp_end or "",
            "line_start": group.line_start,
            "line_end": group.line_end,
            "record_type": "log_event",
        }

    def index_file(
        self,
        collection: "Collection",
        file_path: str,
        content: str,
        project_name: str,
        source_description: str,
        byte_offset: int = 0,
    ) -> tuple[int, int]:
        """Index a log file's content starting from the given byte offset.

        Delegates parsing to LogPipeline.process(), then handles embedding
        and storage:
        1. Run parsing pipeline (boundary → parse → transform → group)
        2. Generate embeddings for group texts
        3. Store chunks in ChromaDB with extended metadata
        4. Return the count of chunks created and the new byte offset

        Args:
            collection: ChromaDB collection to store chunks in.
            file_path: Path of the log file being indexed.
            content: Raw log content (from byte_offset to EOF).
            project_name: Name of the project this file belongs to.
            source_description: Description of the source pattern.
            byte_offset: The byte offset this content starts at in the file.

        Returns:
            Tuple of (chunks_created, new_byte_offset) where new_byte_offset
            is the byte position after the last parsed content.
        """
        if not content or not content.strip():
            return 0, byte_offset

        # Step 1: Run parsing pipeline (boundary → parse → transform → group)
        groups = self._pipeline.process(content, start_offset=0)

        if not groups:
            return 0, byte_offset

        # Step 2: Generate embeddings for group texts
        texts = [g.text for g in groups]
        embeddings = self._embedding_gen.encode(texts)

        # Step 3: Store chunks in ChromaDB with extended metadata
        total_chunks = len(groups)
        ids = []
        metadatas = []
        for idx, group in enumerate(groups):
            chunk_id = f"{file_path}::log_chunk_{byte_offset}_{idx}"
            ids.append(chunk_id)
            metadata = self._build_chunk_metadata(
                group=group,
                file_path=file_path,
                project_name=project_name,
                source_desc=source_description,
                chunk_index=idx,
                total_chunks=total_chunks,
            )
            metadatas.append(metadata)

        # Batch upsert to ChromaDB
        batch_size = 5000
        for start in range(0, len(ids), batch_size):
            end = start + batch_size
            collection.upsert(
                ids=ids[start:end],
                embeddings=embeddings[start:end],
                documents=texts[start:end],
                metadatas=metadatas[start:end],
            )

        # Step 4: Calculate new byte offset
        new_byte_offset = byte_offset + len(content.encode("utf-8"))

        return total_chunks, new_byte_offset

    def index_file_incremental(
        self,
        collection: "Collection",
        file_path: str,
        file_content_bytes: bytes,
        project_name: str,
        source_description: str,
    ) -> tuple[int, int, str | None]:
        """Perform incremental indexing of a log file using stored byte offsets.

        Implements the full incremental indexing logic:
        1. Retrieves the stored byte offset for the file.
        2. Compares the file size with the stored offset.
        3. If file_size < stored_offset: resets offset to 0 (file truncated/rotated).
        4. If no stored offset (0): starts from the beginning.
        5. Reads from offset to EOF, decodes to text.
        6. Calls index_file() to do the actual parsing/indexing.
        7. On success: updates the stored offset (logs error on update failure
           but does NOT rollback stored chunks).
        8. On parse/read error: retains previous offset, returns error message.

        Args:
            collection: ChromaDB collection to store chunks in.
            file_path: Path of the log file being indexed.
            file_content_bytes: Raw bytes of the entire file content.
            project_name: Name of the project this file belongs to.
            source_description: Description of the source pattern.

        Returns:
            Tuple of (chunks_created, new_offset, error_message_or_none).
            - chunks_created: Number of chunks successfully stored.
            - new_offset: The byte offset after the last indexed position.
            - error_message_or_none: None on success, error string on failure.
        """
        file_size = len(file_content_bytes)

        # Step 1: Get stored offset
        stored_offset = self._get_stored_offset(collection, file_path)

        # Step 2-4: Determine effective offset
        if file_size < stored_offset:
            # File was truncated or rotated — re-index from beginning
            offset = 0
        else:
            # Use stored offset (0 if no previous offset)
            offset = stored_offset

        # If offset equals file size, there's nothing new to index
        if offset >= file_size:
            return 0, offset, None

        # Step 5: Read from offset to EOF and decode
        try:
            new_bytes = file_content_bytes[offset:]
            content = new_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as e:
            # On read/decode error: retain previous offset, report error
            error_msg = f"Failed to decode content from offset {offset} in '{file_path}': {e}"
            print(f"  [error] {error_msg}", file=sys.stderr)
            return 0, stored_offset, error_msg

        # Step 6: Call index_file to do the actual indexing
        try:
            chunks_created, new_byte_offset = self.index_file(
                collection=collection,
                file_path=file_path,
                content=content,
                project_name=project_name,
                source_description=source_description,
                byte_offset=offset,
            )
        except Exception as e:
            # On parse/indexing error: retain previous offset, report error
            error_msg = f"Failed to index '{file_path}' from offset {offset}: {e}"
            print(f"  [error] {error_msg}", file=sys.stderr)
            return 0, stored_offset, error_msg

        # Step 7: Update stored offset on success
        if chunks_created > 0:
            try:
                self._store_offset(collection, file_path, new_byte_offset)
            except Exception as e:
                # Log error on offset update failure but do NOT rollback stored chunks
                print(
                    f"  [error] Failed to update stored offset for '{file_path}' "
                    f"(chunks already stored): {e}",
                    file=sys.stderr,
                )
                # Return the new offset even though storage failed — chunks are already stored
                return chunks_created, new_byte_offset, None

        return chunks_created, new_byte_offset, None
