"""Document retrieval tools: get_document, list_projects, list_files, get_project_summary, compare_projects."""

import time

import anyio

from rag_mcp.tools import ToolContext
from rag_mcp.tools.helpers import log_tool_call, validate_path

_ctx: ToolContext | None = None


def register(mcp, ctx: ToolContext) -> None:
    """Register document tools on the MCP server."""
    global _ctx
    _ctx = ctx

    mcp.tool()(get_document)
    mcp.tool()(list_projects)
    mcp.tool()(list_files)
    mcp.tool()(get_project_summary)
    mcp.tool()(compare_projects)


def get_document(file_path: str, project: str) -> str:
    """
    Retrieve all indexed chunks of a specific document.

    Args:
        file_path: Relative file path within the project (e.g., "src/Device.h")
        project: Project name (e.g., "my-project")

    Returns:
        Full reconstructed document content from indexed chunks
    """
    start_time = time.time()

    # Validate path
    path_error = validate_path(file_path)
    if path_error:
        return f"Error: {path_error}"

    if not project:
        return "Error: project is required."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        log_tool_call("get_document", {"file_path": file_path, "project": project}, start_time, 0)
        return f"Error: Project '{project}' not found. Use list_projects() to see available projects."

    results = collection.get(
        where={"file_path": file_path},
        include=["documents", "metadatas"],
    )

    if not results["ids"]:
        log_tool_call("get_document", {"file_path": file_path, "project": project}, start_time, 0)
        return f"Error: Document '{file_path}' not found in project '{project}'."

    # Sort chunks by index and reconstruct
    chunks_with_index = []
    for i, doc in enumerate(results["documents"]):
        meta = results["metadatas"][i]
        chunks_with_index.append((meta.get("chunk_index", 0), doc))

    chunks_with_index.sort(key=lambda x: x[0])
    content = "\n".join(chunk for _, chunk in chunks_with_index)

    log_tool_call("get_document", {"file_path": file_path, "project": project}, start_time, len(chunks_with_index))

    return (
        f"Document: {file_path}\n"
        f"Project: {project}\n"
        f"Chunks: {len(chunks_with_index)}\n"
        f"---\n{content}"
    )


def list_projects() -> str:
    """
    List all indexed projects and their statistics.

    Returns:
        List of projects with document counts and descriptions
    """
    start_time = time.time()

    collections_meta = _ctx.store.list_collections()

    if not collections_meta:
        log_tool_call("list_projects", {}, start_time, 0)
        return "No projects indexed yet. Run 'python indexer.py' to index your projects."

    output_parts = []
    if _ctx.reindex_in_progress():
        output_parts.append("⚠️ Background re-indexing in progress. Stats may be updating.\n")
    output_parts.append("Indexed Projects:\n")
    prefix = _ctx.config.storage.collection_prefix

    for col_meta in collections_meta:
        collection = _ctx.store.client.get_collection(name=col_meta.name)
        count = collection.count()
        project_name = col_meta.name.replace(f"{prefix}_", "", 1)

        # Find project description from config and check if removed
        description = ""
        is_removed = False
        for p in _ctx.config.projects:
            if p.name == project_name:
                description = p.description
                is_removed = p.removed
                break

        # Skip removed projects from listing
        if is_removed:
            continue

        output_parts.append(
            f"  - {project_name}\n"
            f"    Description: {description}\n"
            f"    Indexed chunks: {count}\n"
        )

    log_tool_call("list_projects", {}, start_time, len(collections_meta))
    return "\n".join(output_parts)


def list_files(project: str, file_type: str = "") -> str:
    """
    List all indexed files for a project, optionally filtered by type.

    Args:
        project: Project name (e.g., "my-project")
        file_type: Optional filter: "header", "source", "documentation", "config"

    Returns:
        List of indexed file paths with their types
    """
    start_time = time.time()

    if not project:
        return "Error: project is required."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        log_tool_call("list_files", {"project": project, "file_type": file_type}, start_time, 0)
        return f"Error: Project '{project}' not found."

    # Get all metadata
    where_filter = {"file_type": file_type} if file_type else None
    try:
        results = collection.get(
            where=where_filter,
            include=["metadatas"],
        )
    except Exception as e:
        log_tool_call("list_files", {"project": project, "file_type": file_type}, start_time, 0)
        return f"Error querying collection: {e}"

    if not results["ids"]:
        log_tool_call("list_files", {"project": project, "file_type": file_type}, start_time, 0)
        return f"No files found in project '{project}'" + (
            f" with type '{file_type}'" if file_type else ""
        )

    # Deduplicate by file path
    files = {}
    for meta in results["metadatas"]:
        fp = meta.get("file_path", "unknown")
        if fp not in files:
            files[fp] = meta.get("file_type", "unknown")

    # Sort and format
    sorted_files = sorted(files.items())
    output_parts = [
        f"Files in '{project}'" + (f" (type: {file_type})" if file_type else "") + ":\n"
    ]

    for fp, ft in sorted_files:
        output_parts.append(f"  [{ft}] {fp}")

    output_parts.append(f"\nTotal: {len(sorted_files)} files")

    log_tool_call("list_files", {"project": project, "file_type": file_type}, start_time, len(sorted_files))
    return "\n".join(output_parts)


def get_project_summary(project: str) -> str:
    """
    Get a quick overview of an indexed project: description, file counts by type, total chunks.

    Args:
        project: Project name

    Returns:
        Project summary with statistics
    """
    start_time = time.time()

    if not project:
        return "Error: project is required."

    collection = _ctx.store.get_collection(project)
    if collection is None:
        return f"Error: Project '{project}' not found."

    # Get all metadata
    results = collection.get(include=["metadatas"])

    if not results["ids"]:
        return f"Project '{project}' is indexed but has no chunks."

    # Count by file type
    type_counts = {}
    files_by_type = {}
    for meta in results["metadatas"]:
        ft = meta.get("file_type", "unknown")
        type_counts[ft] = type_counts.get(ft, 0) + 1
        fp = meta.get("file_path", "")
        if ft not in files_by_type:
            files_by_type[ft] = set()
        files_by_type[ft].add(fp)

    # Find description from config
    description = ""
    base_path = ""
    for p in _ctx.config.projects:
        if p.name == project:
            description = p.description
            base_path = p.base_path
            break

    total_chunks = len(results["ids"])
    total_files = len(set(m.get("file_path", "") for m in results["metadatas"]))

    output_parts = [
        f"Project: {project}",
        f"Description: {description}",
        f"Path: {base_path}",
        f"Total files: {total_files}",
        f"Total chunks: {total_chunks}",
        f"\nBreakdown by type:",
    ]

    for ft in sorted(files_by_type.keys()):
        file_count = len(files_by_type[ft])
        chunk_count = type_counts[ft]
        output_parts.append(f"  [{ft}] {file_count} files, {chunk_count} chunks")

    log_tool_call("get_project_summary", {"project": project}, start_time, total_files)
    return "\n".join(output_parts)


async def compare_projects(query: str, project_a: str, project_b: str, top_k: int = 3) -> str:
    """
    Search for the same concept in two projects side by side.
    Useful for comparing implementations across two versions or variants of the same project.

    Args:
        query: What to search for (e.g., "dispense command flow")
        project_a: First project name
        project_b: Second project name
        top_k: Results per project (default: 3)

    Returns:
        Side-by-side results from both projects
    """
    return await anyio.to_thread.run_sync(lambda: _compare_projects_sync(query, project_a, project_b, top_k))


def _compare_projects_sync(query: str, project_a: str, project_b: str, top_k: int = 3) -> str:
    """Synchronous implementation of compare_projects (runs in thread pool)."""
    from rag_mcp.tools.search import _search_docs_sync

    start_time = time.time()

    if not query:
        return "Error: query is required."
    if not project_a or not project_b:
        return "Error: both project_a and project_b are required."

    results_a = _search_docs_sync(query=query, project=project_a, top_k=top_k)
    results_b = _search_docs_sync(query=query, project=project_b, top_k=top_k)

    output = [
        f"Comparing: '{query}'\n",
        f"{'='*60}",
        f"PROJECT A: {project_a}",
        f"{'='*60}",
        results_a,
        f"\n{'='*60}",
        f"PROJECT B: {project_b}",
        f"{'='*60}",
        results_b,
    ]

    log_tool_call("compare_projects", {"query": query, "project_a": project_a, "project_b": project_b}, start_time, 0)
    return "\n".join(output)
