"""
Indexing pipeline orchestrator.

Coordinates file reading, chunking, embedding generation, and ChromaDB storage
for the document indexing workflow.
"""

import glob
import os
import sys
from pathlib import Path

from rich.console import Console

from rag_mcp.chroma_store import ChromaStore
from rag_mcp.chunker import Chunker
from rag_mcp.config_loader import AppConfig, ProjectConfig, SourcePattern
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.file_reader import FileContent, FileReader


class IndexingPipeline:
    """Orchestrates file reading, chunking, embedding, and storage."""

    # Directories to exclude from globbing
    def __init__(
        self,
        config: AppConfig,
        file_reader: FileReader,
        chunker: Chunker,
        embedding_gen: EmbeddingGenerator,
        store: ChromaStore,
        console: Console,
    ):
        self.config = config
        self.file_reader = file_reader
        self.chunker = chunker
        self.embedding_gen = embedding_gen
        self.store = store
        self.console = console

        # Point the reader's PDF cache at <storage.path>/pdf_cache so PDFs are
        # always converted to Markdown, even when the source tree is read-only.
        if getattr(self.file_reader, "pdf_cache_dir", None) is None:
            storage_path = getattr(getattr(config, "storage", None), "path", None)
            if isinstance(storage_path, str):
                try:
                    self.file_reader.pdf_cache_dir = Path(storage_path) / "pdf_cache"
                except Exception:
                    pass

    def index_project(self, project: ProjectConfig, reset: bool = False, is_cancelled: callable = None) -> int:
        """
        Index a single project. Returns total chunks indexed.
        Checks is_cancelled() between files for responsive cancellation.
        Skips log files > 5MB (use index_log_file tool for those).

        Args:
            project: Project configuration to index.
            reset: If True, delete existing collection first.
            is_cancelled: Optional callable returning True if indexing should stop.
        """
        self.console.print(f"\n[bold blue]Indexing: {project.name}[/bold blue]")
        self.console.print(f"  Path: {project.base_path}")

        base_path = Path(project.base_path)
        if not base_path.exists():
            self.console.print(f"  [red]Error: Base path does not exist[/red]")
            return 0

        # Reset collection if requested
        if reset:
            self.store.delete_collection(project.name)
            self.console.print(f"  [yellow]Collection reset[/yellow]")

        collection = self.store.get_or_create_collection(
            project.name, project.description
        )

        total_chunks = 0
        processed_files = 0

        # Collect all files across sources for progress tracking
        excluded_dirs = set(self.config.excluded_dirs)
        all_file_sources = []
        for source in project.sources:
            pattern = str(base_path / source.pattern)
            files = glob.glob(pattern, recursive=True)
            files = [
                f for f in files
                if not any(excluded in Path(f).parts for excluded in excluded_dirs)
            ]
            for f in files:
                all_file_sources.append((f, source))
        total_files = len(all_file_sources)

        MAX_LOG_FILE_SIZE = 5 * 1024 * 1024  # 5 MB — skip larger log files

        for file_idx, (filepath_str, source) in enumerate(all_file_sources, 1):
            # Check cancellation between files
            if is_cancelled and is_cancelled():
                print(
                    f"[reindex] {project.name}: cancelled at {file_idx}/{total_files}",
                    file=sys.stderr,
                )
                break

            filepath = Path(filepath_str)
            if not filepath.is_file():
                continue

            # Skip a PDF when a fresh converted .md sibling exists — the .md is
            # indexed directly via the "**/*.md" pattern, so indexing the PDF too
            # would duplicate content under the same key with a different hash.
            if filepath.suffix.lower() == ".pdf":
                md_sibling = filepath.with_suffix(".md")
                if md_sibling.exists() and md_sibling.stat().st_mtime >= filepath.stat().st_mtime:
                    continue

            # Skip large log files (use index_log_file tool for those)
            if source.type == "log" and filepath.stat().st_size > MAX_LOG_FILE_SIZE:
                continue

            file_content = self.file_reader.read(filepath, base_path)
            if file_content is None:
                continue

            chunks_created = self._process_file(
                collection, file_content, source, project.name
            )
            total_chunks += chunks_created
            processed_files += 1

            # Progress every 20% of files
            if total_files > 10 and file_idx % max(1, total_files // 5) == 0:
                pct = file_idx * 100 // total_files
                print(
                    f"[reindex] {project.name}: {pct}% ({processed_files} files, {total_chunks} chunks)",
                    file=sys.stderr,
                )

        self.console.print(f"  [green]Done: {total_chunks} chunks indexed[/green]")
        return total_chunks

    def prune_project(self, project: ProjectConfig) -> int:
        """
        Remove chunks for files that no longer exist on disk.
        Returns number of files pruned.
        """
        base_path = Path(project.base_path)
        collection = self.store.get_collection(project.name)
        if collection is None:
            return 0

        indexed_files = self.store.get_all_indexed_files(collection)
        pruned = 0

        for relative_path in indexed_files:
            full_path = base_path / relative_path
            if not full_path.exists():
                self.store.delete_file_chunks(collection, relative_path)
                pruned += 1
                self.console.print(
                    f"  [yellow]Pruned: {relative_path}[/yellow]"
                )

        if pruned:
            self.console.print(f"  [green]Pruned {pruned} stale files[/green]")

        return pruned

    def _process_file(
        self, collection, file_content: FileContent, source: SourcePattern, project_name: str
    ) -> int:
        """Process a single file: route to log or text pipeline. Returns chunks created."""
        if source.type == "log":
            return self._process_log_file(
                collection, file_content, source, project_name
            )

        # --- Existing text pipeline (non-log files) ---
        # Check if file already indexed with same hash
        existing_hash = self.store.get_existing_hash(
            collection, file_content.relative_path
        )

        if existing_hash == file_content.file_hash:
            return 0  # File unchanged, skip

        # File changed or new — remove old chunks if they exist
        if existing_hash is not None:
            self.store.delete_file_chunks(collection, file_content.relative_path)

        # Chunk the content
        chunks = self.chunker.chunk(file_content.content)
        if not chunks:
            return 0

        # Generate embeddings
        chunk_texts = [c.content for c in chunks]
        embeddings = self.embedding_gen.encode(chunk_texts)

        # Build metadata
        metadata_base = {
            "file_path": file_content.relative_path,
            "file_type": source.type,
            "file_hash": file_content.file_hash,
            "project": project_name,
            "source_description": source.description,
        }

        # Store in ChromaDB
        self.store.upsert_chunks(
            collection,
            file_content.relative_path,
            chunk_texts,
            embeddings,
            metadata_base,
        )

        return len(chunks)

    def _process_log_file(
        self, collection, file_content: FileContent, source: SourcePattern, project_name: str
    ) -> int:
        """Process a log file through the structured log indexing pipeline.

        Instantiates LogParser, LineFilter, ContentTransform, EventGrouper from
        project config, creates a LogIndexer, and indexes the file with
        incremental byte-offset tracking.

        Args:
            collection: ChromaDB collection to store chunks in.
            file_content: The file content object with path, content, and hash.
            source: The SourcePattern with log_patterns config.
            project_name: Name of the project being indexed.

        Returns:
            Number of chunks created (0 if no parseable lines).
        """
        from rag_mcp.log.parsing.content_transform import ContentTransform
        from rag_mcp.log.parsing.event_grouper import EventGrouper
        from rag_mcp.log.parsing.line_filter import LineFilter
        from rag_mcp.log.log_indexer import LogIndexer
        from rag_mcp.log.parsing.log_parser import LogParser

        # Get project's log_settings (find the project config)
        project_config = self._find_project_config(project_name)
        log_settings = project_config.log_settings if project_config else None

        # Build LogSettings with defaults if not configured
        from rag_mcp.log.parsing.config_models import LogSettings

        settings = log_settings if log_settings is not None else LogSettings()

        # Get log_patterns from source (empty list triggers default patterns in LogParser)
        log_patterns = source.log_patterns if source.log_patterns else None

        # Instantiate LineFilter from log_settings.line_filters
        line_filter = LineFilter(
            filters=settings.line_filters,
            default_action=settings.default_filter_action,
        )

        # Instantiate ContentTransform from log_settings.content_transforms
        content_transform = ContentTransform(transforms=settings.content_transforms)

        # Instantiate LogParser with patterns, settings, severity_mapping, line_filter
        log_parser = LogParser(
            patterns=log_patterns,
            settings=settings,
            severity_mapping=settings.severity_mapping or None,
            line_filter=line_filter,
            filename=file_content.relative_path,
        )

        # Instantiate EventGrouper with settings and grouping_rules
        event_grouper = EventGrouper(
            settings=settings,
            grouping_rules=settings.grouping_rules or None,
        )

        # Create LogIndexer with all components
        log_indexer = LogIndexer(
            log_parser=log_parser,
            content_transform=content_transform,
            event_grouper=event_grouper,
            embedding_gen=self.embedding_gen,
            store=self.store,
        )

        # Get stored offset for incremental indexing
        stored_offset = log_indexer._get_stored_offset(
            collection, file_content.relative_path
        )

        # Check for file truncation: if file size < stored offset, reset
        file_size = len(file_content.content.encode("utf-8"))
        if file_size < stored_offset:
            stored_offset = 0

        # Read content from offset
        if stored_offset > 0:
            content_bytes = file_content.content.encode("utf-8")
            content_from_offset = content_bytes[stored_offset:].decode("utf-8", errors="replace")
        else:
            content_from_offset = file_content.content

        # Handle empty content
        if not content_from_offset or not content_from_offset.strip():
            return 0

        # Index the file content
        try:
            chunks_created, new_offset = log_indexer.index_file(
                collection=collection,
                file_path=file_content.relative_path,
                content=content_from_offset,
                project_name=project_name,
                source_description=source.description,
                byte_offset=stored_offset,
            )
        except Exception as e:
            # On parse/read error: retain previous offset, report error
            print(
                f"WARNING: Error indexing log file '{file_content.relative_path}': {e}",
                file=sys.stderr,
            )
            return 0

        # Handle file with no parseable lines
        if chunks_created == 0:
            print(
                f"WARNING: No parseable log lines in '{file_content.relative_path}', skipping",
                file=sys.stderr,
            )
            return 0

        # Store the new byte offset for incremental indexing
        try:
            log_indexer._store_offset(
                collection, file_content.relative_path, new_offset
            )
        except Exception as e:
            # Offset update failure: log error but don't rollback stored chunks
            print(
                f"WARNING: Failed to update offset for '{file_content.relative_path}': {e}",
                file=sys.stderr,
            )

        return chunks_created

    def _find_project_config(self, project_name: str) -> ProjectConfig | None:
        """Find a ProjectConfig by name from the app config.

        Args:
            project_name: The project name to look up.

        Returns:
            The matching ProjectConfig, or None if not found.
        """
        for project in self.config.projects:
            if project.name == project_name:
                return project
        return None
