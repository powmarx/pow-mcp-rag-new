"""Project/file/folder management tools: add, remove, clear."""

import sys
import time
from pathlib import Path

import anyio

from rag_mcp.tools import ToolContext
from rag_mcp.tools.helpers import FILE_TYPE_MAP, log_tool_call

_ctx: ToolContext | None = None


def register(mcp, ctx: ToolContext) -> None:
    """Register management tools on the MCP server."""
    global _ctx
    _ctx = ctx

    mcp.tool()(add_project)
    mcp.tool()(add_file)
    mcp.tool()(add_folder)
    mcp.tool()(add_pattern)
    mcp.tool()(remove_project)
    mcp.tool()(clear_project_index)
    mcp.tool()(remove_file_from_index)


async def add_project(name: str, path: str) -> str:
    """
    Add a new project to the RAG index. Auto-detects source patterns and indexes immediately.

    Args:
        name: Unique project name (e.g., "my-new-api")
        path: Absolute path to the project root directory

    Returns:
        Summary of detected patterns and indexing result
    """
    return await anyio.to_thread.run_sync(lambda: _add_project_sync(name, path))


def _add_project_sync(name: str, path: str) -> str:
    """Synchronous implementation of add_project (runs in thread pool)."""
    start_time = time.time()

    if not name or not name.strip():
        return "Error: name is required."
    if not path or not path.strip():
        return "Error: path is required."

    project_path = Path(path)
    if not project_path.exists():
        return f"Error: Path does not exist: {path}"
    if not project_path.is_dir():
        return f"Error: Path is not a directory: {path}"

    existing_names = {p.name for p in _ctx.config.projects}
    if name in existing_names:
        # Check if it's a removed project — re-activate it
        for p in _ctx.config.projects:
            if p.name == name and p.removed:
                p.removed = False
                _ctx.loader.save(_ctx.config)
                log_tool_call("add_project", {"name": name, "path": path}, start_time, 0)
                return (
                    f"Project '{name}' re-activated (was previously removed).\n"
                    f"  Configuration preserved. Run index_log_file or reindex to populate data."
                )
        return f"Error: Project '{name}' already exists. Use a different name."

    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import ProjectConfig
    from rag_mcp.file_reader import FileReader
    from rag_mcp.indexing_pipeline import IndexingPipeline
    from rag_mcp.source_scanner import build_source_patterns

    # Same config-driven file-type patterns as discovery (code + docs/specs).
    sources = build_source_patterns(project_path, _ctx.config)

    if not sources:
        return f"No code or doc/spec files found in: {path}"

    new_project = ProjectConfig(
        name=name,
        description=f"Added via MCP: {project_path.name}",
        base_path=str(project_path).replace("\\", "/"),
        sources=sources,
    )

    _ctx.config.projects.append(new_project)
    _ctx.loader.save(_ctx.config)

    class SilentConsole:
        def print(self, *args, **kwargs):
            pass

    file_reader = FileReader()
    chunker = Chunker(_ctx.config.chunking)
    pipeline = IndexingPipeline(
        config=_ctx.config,
        file_reader=file_reader,
        chunker=chunker,
        embedding_gen=_ctx.embedding_gen,
        store=_ctx.store,
        console=SilentConsole(),
    )

    # PDFs are converted to Markdown automatically during indexing (into the
    # configured PDF cache under storage.path), so no separate step is needed.
    chunks = pipeline.index_project(new_project)

    output_parts = [
        f"Project '{name}' added and indexed successfully!\n",
        f"Path: {path}",
        f"Patterns detected: {len(sources)}",
        f"Chunks indexed: {chunks}\n",
        "Detected source patterns:",
    ]
    for s in sources:
        output_parts.append(f"  [{s.type}] {s.pattern} - {s.description}")

    log_tool_call("add_project", {"name": name, "path": path}, start_time, chunks)
    return "\n".join(output_parts)


async def add_file(file_path: str, project: str) -> str:
    """
    Index a specific file into an existing project and persist it in config.yaml
    so it gets re-indexed automatically on future runs.

    Args:
        file_path: Absolute path to the file to index
        project: Project name to add the file to (must already exist)

    Returns:
        Confirmation with chunk count, or error message
    """
    return await anyio.to_thread.run_sync(lambda: _add_file_sync(file_path, project))


def _add_file_sync(file_path: str, project: str) -> str:
    """Synchronous implementation of add_file (runs in thread pool)."""
    start_time = time.time()

    if not file_path or not file_path.strip():
        return "Error: file_path is required."
    if not project or not project.strip():
        return "Error: project is required."

    target = Path(file_path)
    if not target.exists():
        return f"Error: File does not exist: {file_path}"
    if not target.is_file():
        return f"Error: Path is not a file: {file_path}"

    # Find the project in config
    project_config = None
    for p in _ctx.config.projects:
        if p.name == project:
            project_config = p
            break

    if project_config is None:
        return f"Error: Project '{project}' not found. Use list_projects() to see available projects."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        return f"Error: Project '{project}' has no index. Run the indexer first."

    # Read the file
    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import SourcePattern
    from rag_mcp.file_reader import FileReader

    file_reader = FileReader(pdf_cache_dir=Path(_ctx.config.storage.path) / "pdf_cache")
    base_path = Path(project_config.base_path)

    # Determine relative path
    try:
        relative = target.relative_to(base_path)
        relative_str = str(relative).replace("\\", "/")
    except ValueError:
        relative_str = target.name

    content = file_reader.read(target, base_path if target.is_relative_to(base_path) else target.parent)
    if content is None:
        return f"Error: Could not read file (binary or encoding issue): {file_path}"

    # Chunk and embed
    chunker = Chunker(_ctx.config.chunking)
    chunks = chunker.chunk(content.content)
    if not chunks:
        return f"Error: File produced no chunks (empty content): {file_path}"

    _ctx.ensure_model_loaded()

    chunk_texts = [c.content for c in chunks]
    embeddings = _ctx.embedding_gen.encode(chunk_texts)

    # Determine file type from extension
    ext = target.suffix.lower()
    file_type = FILE_TYPE_MAP.get(ext, "source")

    # Delete existing chunks for this file (if re-adding)
    _ctx.store.delete_file_chunks(collection, relative_str)

    # Store
    metadata_base = {
        "file_path": relative_str,
        "file_type": file_type,
        "file_hash": content.file_hash,
        "project": project,
        "source_description": f"Manually added: {target.name}",
    }
    _ctx.store.upsert_chunks(collection, relative_str, chunk_texts, embeddings, metadata_base)

    # Persist the file pattern in config.yaml so it gets re-indexed
    pattern_str = relative_str
    existing_patterns = {s.pattern for s in project_config.sources}
    if pattern_str not in existing_patterns:
        project_config.sources.append(SourcePattern(
            pattern=pattern_str,
            type=file_type,
            description=f"Manually added: {target.name}",
        ))
        _ctx.loader.save(_ctx.config)

    log_tool_call("add_file", {"file_path": file_path, "project": project}, start_time, len(chunks))
    return (
        f"File indexed and saved to config!\n"
        f"  File: {relative_str}\n"
        f"  Project: {project}\n"
        f"  Type: {file_type}\n"
        f"  Chunks: {len(chunks)}\n"
        f"  Persisted: will be re-indexed automatically"
    )


async def add_folder(folder_path: str, project: str, pattern: str = "**/*") -> str:
    """
    Index all files in a folder into an existing project and persist the pattern
    in config.yaml so the folder gets re-indexed automatically on future runs.

    Args:
        folder_path: Absolute path to the folder to index
        project: Project name to add the folder to (must already exist)
        pattern: Glob pattern for files within the folder (default: "**/*" for all files)

    Returns:
        Confirmation with file/chunk counts, or error message
    """
    return await anyio.to_thread.run_sync(lambda: _add_folder_sync(folder_path, project, pattern))


def _add_folder_sync(folder_path: str, project: str, pattern: str = "**/*") -> str:
    """Synchronous implementation of add_folder (runs in thread pool)."""
    start_time = time.time()

    if not folder_path or not folder_path.strip():
        return "Error: folder_path is required."
    if not project or not project.strip():
        return "Error: project is required."

    target_dir = Path(folder_path)
    if not target_dir.exists():
        return f"Error: Folder does not exist: {folder_path}"
    if not target_dir.is_dir():
        return f"Error: Path is not a folder: {folder_path}"

    # Find the project in config
    project_config = None
    for p in _ctx.config.projects:
        if p.name == project:
            project_config = p
            break

    if project_config is None:
        return f"Error: Project '{project}' not found. Use list_projects() to see available projects."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        return f"Error: Project '{project}' has no index. Run the indexer first."

    import glob as glob_mod

    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import SourcePattern
    from rag_mcp.file_reader import FileReader

    file_reader = FileReader(pdf_cache_dir=Path(_ctx.config.storage.path) / "pdf_cache")
    base_path = Path(project_config.base_path)

    # Determine relative folder path
    try:
        relative_dir = target_dir.relative_to(base_path)
        relative_dir_str = str(relative_dir).replace("\\", "/")
    except ValueError:
        return f"Error: Folder must be inside the project base_path ({base_path})"

    # Build the full glob pattern
    full_pattern = str(target_dir / pattern)
    files = glob_mod.glob(full_pattern, recursive=True)
    files = [f for f in files if Path(f).is_file()]

    if not files:
        return f"Error: No files found matching pattern '{pattern}' in {folder_path}"

    _ctx.ensure_model_loaded()

    chunker = Chunker(_ctx.config.chunking)
    total_chunks = 0
    indexed_files = 0

    for filepath_str in files:
        filepath = Path(filepath_str)
        content = file_reader.read(filepath, base_path)
        if content is None:
            continue

        chunks = chunker.chunk(content.content)
        if not chunks:
            continue

        chunk_texts = [c.content for c in chunks]
        embeddings = _ctx.embedding_gen.encode(chunk_texts)

        ext = filepath.suffix.lower()
        file_type = FILE_TYPE_MAP.get(ext, "source")

        _ctx.store.delete_file_chunks(collection, content.relative_path)

        metadata_base = {
            "file_path": content.relative_path,
            "file_type": file_type,
            "file_hash": content.file_hash,
            "project": project,
            "source_description": f"Manually added folder: {relative_dir_str}",
        }
        _ctx.store.upsert_chunks(collection, content.relative_path, chunk_texts, embeddings, metadata_base)
        total_chunks += len(chunks)
        indexed_files += 1

    # Persist the folder pattern in config.yaml
    config_pattern = f"{relative_dir_str}/{pattern}"
    existing_patterns = {s.pattern for s in project_config.sources}
    if config_pattern not in existing_patterns:
        project_config.sources.append(SourcePattern(
            pattern=config_pattern,
            type="source",
            description=f"Manually added folder: {relative_dir_str}",
        ))
        _ctx.loader.save(_ctx.config)

    log_tool_call("add_folder", {"folder_path": folder_path, "project": project, "pattern": pattern}, start_time, total_chunks)
    return (
        f"Folder indexed and saved to config!\n"
        f"  Folder: {relative_dir_str}\n"
        f"  Pattern: {config_pattern}\n"
        f"  Project: {project}\n"
        f"  Files indexed: {indexed_files}\n"
        f"  Chunks: {total_chunks}\n"
        f"  Persisted: will be re-indexed automatically"
    )


async def add_pattern(
    project: str,
    pattern: str,
    type: str = "documentation",
    description: str = "",
) -> str:
    """
    Add a glob pattern to an existing project and index matching files immediately.
    Persists the pattern in config.yaml so it gets re-indexed automatically on future runs.

    Use this when you want to index a specific set of files by pattern rather than
    pointing at a concrete folder path. The pattern is relative to the project base_path.

    Args:
        project: Project name to add the pattern to (must already exist)
        pattern: Glob pattern relative to project base_path (e.g., "doc/specifications/**/*.md")
        type: File type classification — one of: source, header, documentation, config (default: documentation)
        description: Optional human-readable description for this source entry

    Returns:
        Confirmation with file/chunk counts, or error message

    Examples:
        add_pattern("my-project", "doc/specifications/**/*.md", "documentation", "Hardware specs")
        add_pattern("my-project", "src/**/*.json", "config", "JSON config files")
    """
    return await anyio.to_thread.run_sync(lambda: _add_pattern_sync(project, pattern, type, description))


def _add_pattern_sync(
    project: str,
    pattern: str,
    type: str = "documentation",
    description: str = "",
) -> str:
    """Synchronous implementation of add_pattern (runs in thread pool)."""
    import glob as glob_mod

    start_time = time.time()

    # --- Validate inputs ---
    if not project or not project.strip():
        return "Error: project is required."
    if not pattern or not pattern.strip():
        return "Error: pattern is required."

    project = project.strip()
    pattern = pattern.strip()

    valid_types = {"source", "header", "documentation", "config"}
    if type not in valid_types:
        return f"Error: type must be one of: {', '.join(sorted(valid_types))}. Got '{type}'."

    # --- Find the project ---
    project_config = None
    for p in _ctx.config.projects:
        if p.name == project:
            project_config = p
            break

    if project_config is None:
        return f"Error: Project '{project}' not found. Use list_projects() to see available projects."

    if project_config.removed:
        return f"Error: Project '{project}' is marked as removed. Re-add it first with add_project()."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        return f"Error: Project '{project}' has no index. Run the indexer first."

    # --- Resolve files matching the pattern ---
    base_path = Path(project_config.base_path)
    full_pattern = str(base_path / pattern)
    matched = glob_mod.glob(full_pattern, recursive=True)
    files = [Path(f) for f in matched if Path(f).is_file()]

    if not files:
        # Pattern is valid syntax but matches nothing — still persist it (files may appear later)
        from rag_mcp.config_loader import SourcePattern
        existing_patterns = {s.pattern for s in project_config.sources}
        if pattern not in existing_patterns:
            desc = description or f"Manually added pattern: {pattern}"
            project_config.sources.append(SourcePattern(pattern=pattern, type=type, description=desc))
            _ctx.loader.save(_ctx.config)
            return (
                f"Pattern saved to config (no files matched yet).\n"
                f"  Project: {project}\n"
                f"  Pattern: {pattern}\n"
                f"  Type: {type}\n"
                f"  Note: No files matched '{full_pattern}' right now.\n"
                f"        The pattern is persisted and will be picked up when files appear."
            )
        else:
            return (
                f"Pattern already exists in config (no files matched).\n"
                f"  Project: {project}\n"
                f"  Pattern: {pattern}"
            )

    # --- Index matching files ---
    from rag_mcp.chunker import Chunker
    from rag_mcp.config_loader import SourcePattern
    from rag_mcp.file_reader import FileReader

    _ctx.ensure_model_loaded()
    file_reader = FileReader(pdf_cache_dir=Path(_ctx.config.storage.path) / "pdf_cache")
    chunker = Chunker(_ctx.config.chunking)

    desc = description or f"Manually added pattern: {pattern}"
    total_chunks = 0
    indexed_files = 0
    skipped_files = []

    for filepath in files:
        content = file_reader.read(filepath, base_path)
        if content is None:
            skipped_files.append(filepath.name)
            continue

        chunks = chunker.chunk(content.content)
        if not chunks:
            skipped_files.append(filepath.name)
            continue

        chunk_texts = [c.content for c in chunks]
        embeddings = _ctx.embedding_gen.encode(chunk_texts)

        from rag_mcp.tools.helpers import FILE_TYPE_MAP
        file_type_for_chunk = FILE_TYPE_MAP.get(filepath.suffix.lower(), type)

        _ctx.store.delete_file_chunks(collection, content.relative_path)

        metadata_base = {
            "file_path": content.relative_path,
            "file_type": file_type_for_chunk,
            "file_hash": content.file_hash,
            "project": project,
            "source_description": desc,
        }
        _ctx.store.upsert_chunks(
            collection, content.relative_path, chunk_texts, embeddings, metadata_base
        )
        total_chunks += len(chunks)
        indexed_files += 1

    # --- Persist pattern in config.yaml ---
    existing_patterns = {s.pattern for s in project_config.sources}
    pattern_is_new = pattern not in existing_patterns
    if pattern_is_new:
        project_config.sources.append(SourcePattern(pattern=pattern, type=type, description=desc))
        _ctx.loader.save(_ctx.config)

    log_tool_call(
        "add_pattern",
        {"project": project, "pattern": pattern, "type": type},
        start_time,
        total_chunks,
    )

    lines = [
        f"Pattern indexed and saved to config!\n",
        f"  Project:  {project}",
        f"  Pattern:  {pattern}",
        f"  Type:     {type}",
        f"  Files:    {indexed_files} indexed",
        f"  Chunks:   {total_chunks}",
        f"  Config:   {'added (new)' if pattern_is_new else 'already present (updated index)'}",
    ]
    if skipped_files:
        lines.append(f"  Skipped:  {len(skipped_files)} (binary/unreadable): {', '.join(skipped_files[:5])}")
    lines.append(f"  Persisted: will be re-indexed automatically on future runs")

    return "\n".join(lines)


def remove_project(name: str) -> str:
    """
    Remove a project from the RAG index. Deletes all indexed chunks and marks
    the project as removed in config.yaml (preserving configuration for future re-add).

    Args:
        name: Project name to remove (e.g., "my_device_logs")

    Returns:
        Confirmation of removal with chunk count deleted.
    """
    start_time = time.time()

    if not name or not name.strip():
        return "Error: name is required."

    name = name.strip()

    # Find the project in config
    project_config = None
    for p in _ctx.config.projects:
        if p.name == name:
            project_config = p
            break

    if project_config is None:
        return f"Error: Project '{name}' not found. Use list_projects() to see available projects."

    # Delete the ChromaDB collection
    chunks_deleted = 0
    try:
        collection = _ctx.store.get_collection(name)
        if collection is not None:
            chunks_deleted = collection.count()
            _ctx.store.delete_collection(name)
    except Exception as e:
        print(f"[remove_project] Error deleting collection: {e}", file=sys.stderr)

    # Mark as removed (soft-delete — preserve config for future re-add)
    project_config.removed = True

    # Save config
    try:
        _ctx.loader.save(_ctx.config)
    except Exception as e:
        return f"Collection deleted ({chunks_deleted} chunks) but failed to update config.yaml: {e}"

    log_tool_call("remove_project", {"name": name}, start_time, chunks_deleted)

    return (
        f"Project '{name}' removed successfully.\n"
        f"  Chunks deleted: {chunks_deleted}\n"
        f"  Config updated: marked as removed (configuration preserved)"
    )


def clear_project_index(name: str) -> str:
    """
    Clear all indexed data for a project without removing it from config.yaml.
    The project remains configured and can be re-indexed later.

    Args:
        name: Project name to clear (e.g., "my_device_logs")

    Returns:
        Confirmation with number of chunks deleted.
    """
    start_time = time.time()

    if not name or not name.strip():
        return "Error: name is required."

    name = name.strip()

    # Verify project exists in config
    project_exists = any(p.name == name for p in _ctx.config.projects)
    if not project_exists:
        return f"Error: Project '{name}' not found. Use list_projects() to see available projects."

    # Delete the ChromaDB collection
    chunks_deleted = 0
    try:
        collection = _ctx.store.get_collection(name)
        if collection is not None:
            chunks_deleted = collection.count()
            _ctx.store.delete_collection(name)
    except Exception as e:
        return f"Error deleting collection: {e}"

    log_tool_call("clear_project_index", {"name": name}, start_time, chunks_deleted)

    return (
        f"Index cleared for project '{name}'.\n"
        f"  Chunks deleted: {chunks_deleted}\n"
        f"  Project remains in config.yaml (ready for re-indexing)."
    )


def remove_file_from_index(file_path: str, project: str) -> str:
    """
    Remove all indexed chunks for a specific file from a project's index.
    Useful for removing large log files or outdated files without clearing the entire project.

    Args:
        file_path: Relative file path within the project (e.g., "device-26-04-28.log")
        project: Project name (e.g., "my_device_logs")

    Returns:
        Confirmation with number of chunks removed.
    """
    start_time = time.time()

    if not file_path or not file_path.strip():
        return "Error: file_path is required."
    if not project or not project.strip():
        return "Error: project is required."

    file_path = file_path.strip()
    project = project.strip()

    # Get the collection
    collection = _ctx.store.get_collection(project)
    if collection is None:
        return f"Error: Project '{project}' not found or not indexed. Use list_projects() to see available projects."

    # Find chunks matching the file path (try exact match and partial match)
    chunks_deleted = 0
    try:
        # Try exact match first
        results = collection.get(
            where={"file_path": file_path},
            include=[],
        )
        if not results["ids"]:
            # Try partial match (file name only, without path prefix)
            all_results = collection.get(include=["metadatas"])
            matching_ids = [
                id_ for id_, meta in zip(all_results["ids"], all_results["metadatas"])
                if meta.get("file_path", "").endswith(file_path)
                or file_path in meta.get("file_path", "")
            ]
            if matching_ids:
                collection.delete(ids=matching_ids)
                chunks_deleted = len(matching_ids)
        else:
            collection.delete(ids=results["ids"])
            chunks_deleted = len(results["ids"])
    except Exception as e:
        return f"Error removing chunks: {e}"

    log_tool_call("remove_file_from_index", {"file_path": file_path, "project": project}, start_time, chunks_deleted)

    if chunks_deleted == 0:
        return f"No chunks found for file '{file_path}' in project '{project}'."

    return (
        f"Removed '{file_path}' from project '{project}' index.\n"
        f"  Chunks deleted: {chunks_deleted}"
    )
