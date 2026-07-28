"""Search tools: semantic search, hex pattern, variable/function lookup."""

import time

import anyio

from rag_mcp.tools import ToolContext
from rag_mcp.tools.helpers import extract_snippet, log_tool_call

_ctx: ToolContext | None = None


def register(mcp, ctx: ToolContext) -> None:
    """Register search tools on the MCP server."""
    global _ctx
    _ctx = ctx

    mcp.tool()(search_docs)
    mcp.tool()(search_specs)
    mcp.tool()(search_code)
    mcp.tool()(search_hex_pattern)
    mcp.tool()(find_variable)
    mcp.tool()(find_function)


async def search_docs(query: str, project: str = "", top_k: int = 5, file_type: str = "") -> str:
    """
    Semantic search across indexed project documentation.

    Args:
        query: Natural language search query (e.g., "how does dispense rejection work")
        project: Optional project name to filter results. Leave empty to search all projects.
        top_k: Number of results to return (default: 5, max: 20)
        file_type: Optional filter by file type: "header", "source", "documentation", "config"

    Returns:
        Relevant document chunks with metadata (file path, project, type)
    """
    return await anyio.to_thread.run_sync(lambda: _search_docs_sync(query, project, top_k, file_type))


def _search_docs_sync(query: str, project: str = "", top_k: int = 5, file_type: str = "") -> str:
    """Synchronous implementation of search_docs (runs in thread pool)."""
    start_time = time.time()

    # Validate query
    if not query or not query.strip():
        return "Error: query must not be empty."
    if len(query) > 1000:
        return "Error: query must not exceed 1000 characters."

    # Clamp top_k
    top_k = min(max(1, top_k), 20)

    # Generate query embedding (lazy load model on first use)
    try:
        _ctx.ensure_model_loaded()
        query_embedding = _ctx.embedding_gen.encode_query(query)
    except Exception as e:
        return f"Error encoding query: {e}"

    # When reranking is enabled, over-fetch candidates from the bi-encoder
    # vector search so the cross-encoder has a wider pool to rerank from.
    reranking_enabled = bool(_ctx.config.reranker.enabled and _ctx.reranker is not None)
    fetch_k = top_k * _ctx.config.reranker.overfetch_factor if reranking_enabled else top_k
    fetch_k = min(fetch_k, 100)

    results = []

    # Determine which collections to search
    if project:
        collection = _ctx.store.get_collection(project)
        if collection is None:
            return f"Error: Project '{project}' not found. Use list_projects() to see available projects."
        collections = [collection]
    else:
        collections_meta = _ctx.store.list_collections()
        collections = []
        for col_meta in collections_meta:
            try:
                collections.append(_ctx.store.client.get_collection(name=col_meta.name))
            except Exception:
                continue

    if not collections:
        return "No indexed collections found. Run 'python indexer.py' first."

    for collection in collections:
        # Build where filter
        where_filter = None
        if file_type:
            where_filter = {"file_type": file_type}

        try:
            query_results = collection.query(
                query_embeddings=[query_embedding],
                n_results=fetch_k,
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            continue

        if query_results["documents"] and query_results["documents"][0]:
            for i, doc in enumerate(query_results["documents"][0]):
                meta = query_results["metadatas"][0][i]
                distance = query_results["distances"][0][i]
                results.append(
                    {
                        "content": doc,
                        "file_path": meta.get("file_path", "unknown"),
                        "project": meta.get("project", "unknown"),
                        "file_type": meta.get("file_type", "unknown"),
                        "relevance": round(1 - distance, 4),
                        "description": meta.get("source_description", ""),
                    }
                )

    # Sort by bi-encoder relevance and cap the pool before reranking (keeps
    # cross-encoder cost bounded when many collections are searched at once).
    results.sort(key=lambda x: x["relevance"], reverse=True)
    results = results[:fetch_k]

    if reranking_enabled and results:
        try:
            _ctx.ensure_reranker_loaded()
            scores = _ctx.reranker.rerank(query, [r["content"] for r in results])
            for r, score in zip(results, scores):
                r["relevance"] = round(float(score), 4)
            results.sort(key=lambda x: x["relevance"], reverse=True)
        except Exception as e:
            # Reranking is a best-effort enhancement — fall back to bi-encoder
            # ordering (already sorted above) rather than failing the search.
            print(f"[search] Reranking failed, using vector-search order: {e}", file=__import__("sys").stderr)

    results = results[:top_k]

    log_tool_call("search_docs", {"query": query, "project": project, "top_k": top_k, "file_type": file_type}, start_time, len(results))

    if not results:
        return f"No results found for query: '{query}'"

    # Format output
    output_parts = []

    # Warn if reindex is in progress
    if _ctx.reindex_in_progress():
        output_parts.append(
            "⚠️ Note: Background re-indexing is in progress. "
            "Results may not include the latest file changes.\n"
        )

    output_parts.append(f"Found {len(results)} results for: '{query}'\n")
    for i, r in enumerate(results, 1):
        output_parts.append(
            f"--- Result {i} (relevance: {r['relevance']}) ---\n"
            f"Project: {r['project']}\n"
            f"File: {r['file_path']} ({r['file_type']})\n"
            f"Description: {r['description']}\n"
            f"Content:\n{r['content']}\n"
        )

    return "\n".join(output_parts)


async def search_specs(query: str, project: str = "", top_k: int = 5) -> str:
    """
    Search only specification and documentation files (filters out source code).
    Use when you need "what does the spec say about X" without code noise.

    Args:
        query: Natural language search query
        project: Optional project name filter
        top_k: Number of results (default: 5, max: 20)

    Returns:
        Relevant documentation chunks (specs, requirements, design docs, PDFs)
    """
    return await anyio.to_thread.run_sync(lambda: _search_docs_sync(query, project, top_k, "documentation"))


async def search_code(query: str, project: str = "", top_k: int = 5, headers_only: bool = False) -> str:
    """
    Search only source code and header files (filters out documentation).
    Use when you need implementation details.

    Args:
        query: Natural language search query
        project: Optional project name filter
        top_k: Number of results (default: 5, max: 20)
        headers_only: If true, search only header files (API contracts)

    Returns:
        Relevant source code chunks
    """
    return await anyio.to_thread.run_sync(lambda: _search_code_sync(query, project, top_k, headers_only))


def _search_code_sync(query: str, project: str = "", top_k: int = 5, headers_only: bool = False) -> str:
    """Synchronous implementation of search_code (runs in thread pool)."""
    # Search both headers and source if not headers_only
    if not headers_only:
        results_src = _search_docs_sync(query=query, project=project, top_k=top_k, file_type="source")
        results_hdr = _search_docs_sync(query=query, project=project, top_k=top_k, file_type="header")
        # Combine and return (simple concatenation, both already formatted)
        if "No results" in results_src and "No results" in results_hdr:
            return f"No code results found for: '{query}'"
        parts = []
        if "No results" not in results_hdr:
            parts.append(results_hdr)
        if "No results" not in results_src:
            parts.append(results_src)
        return "\n".join(parts)
    return _search_docs_sync(query=query, project=project, top_k=top_k, file_type="header")


async def find_function(function_name: str, project: str = "") -> str:
    """
    Find where a function is defined (headers) and used (source files).

    Args:
        function_name: Name of the function to find (e.g., "StoreMoney", "CmdDispense")
        project: Optional project name filter

    Returns:
        Header declarations and source usages of the function
    """
    return await anyio.to_thread.run_sync(lambda: _find_function_sync(function_name, project))


def _find_function_sync(function_name: str, project: str = "") -> str:
    """Synchronous implementation of find_function (runs in thread pool)."""
    start_time = time.time()

    if not function_name or not function_name.strip():
        return "Error: function_name is required."

    results_parts = [f"Searching for function: '{function_name}'\n"]

    # Search in headers (declarations)
    header_results = _search_docs_sync(
        query=f"{function_name} function declaration",
        project=project, top_k=3, file_type="header"
    )
    if "No results" not in header_results:
        results_parts.append("=== Declarations (headers) ===")
        results_parts.append(header_results)

    # Search in source (implementations/calls)
    source_results = _search_docs_sync(
        query=f"{function_name} implementation call",
        project=project, top_k=5, file_type="source"
    )
    if "No results" not in source_results:
        results_parts.append("\n=== Implementations/Calls (source) ===")
        results_parts.append(source_results)

    # Also try text search for exact name matches
    all_collections = _ctx.store.list_collections()
    prefix = _ctx.config.storage.collection_prefix
    exact_matches = []

    for col_meta in all_collections:
        if project:
            proj_name = col_meta.name.replace(f"{prefix}_", "", 1)
            if proj_name != project:
                continue
        try:
            col = _ctx.store.client.get_collection(name=col_meta.name)
            text_results = col.get(
                where_document={"$contains": function_name},
                include=["metadatas"],
            )
            if text_results["ids"]:
                for meta in text_results["metadatas"]:
                    fp = meta.get("file_path", "")
                    if fp not in exact_matches:
                        exact_matches.append(fp)
        except Exception:
            continue

    if exact_matches:
        results_parts.append(f"\n=== Files containing '{function_name}' ===")
        for fp in sorted(set(exact_matches))[:15]:
            results_parts.append(f"  {fp}")

    log_tool_call("find_function", {"function_name": function_name, "project": project}, start_time, len(exact_matches))
    return "\n".join(results_parts)


def search_hex_pattern(pattern: str, project: str = "", top_k: int = 10) -> str:
    """
    Search for hex error codes or patterns in indexed documents using text matching.
    Supports partial matches (e.g., '88a153' finds 88a15300, 88a15310, etc.)
    Use this for device error codes, packet IDs, or any hex value lookup.

    Args:
        pattern: Hex pattern to search for (e.g., "88a153", "0x0521", "ERROR_118")
        project: Optional project name to filter results. Leave empty to search all.
        top_k: Maximum results to return (default: 10)

    Returns:
        Document chunks containing the hex pattern with file and project info
    """
    start_time = time.time()

    if not pattern or not pattern.strip():
        return "Error: pattern is required."

    search_term = pattern.strip().lower().replace("0x", "").replace(" ", "")
    original_pattern = pattern.strip()

    # Determine which collections to search
    if project:
        collection = _ctx.store.get_collection(project)
        if collection is None:
            return f"Error: Project '{project}' not found."
        collections_to_search = [(project, collection)]
    else:
        collections_meta = _ctx.store.list_collections()
        prefix = _ctx.config.storage.collection_prefix
        collections_to_search = []
        for col_meta in collections_meta:
            try:
                col = _ctx.store.client.get_collection(name=col_meta.name)
                proj_name = col_meta.name.replace(f"{prefix}_", "", 1)
                collections_to_search.append((proj_name, col))
            except Exception:
                continue

    if not collections_to_search:
        return "No indexed collections found."

    results = []

    for proj_name, col in collections_to_search:
        search_variants = [
            original_pattern,
            search_term,
            original_pattern.upper(),
            f"0x{search_term}",
            f"0x{search_term.upper()}",
            f"${search_term.upper()}",
        ]

        seen_ids = set()
        for variant in search_variants:
            if not variant:
                continue
            try:
                query_results = col.get(
                    where_document={"$contains": variant},
                    include=["documents", "metadatas"],
                )
            except Exception:
                continue

            if not query_results["ids"]:
                continue

            for i, doc_id in enumerate(query_results["ids"]):
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                doc = query_results["documents"][i]
                meta = query_results["metadatas"][i]
                snippet = extract_snippet(doc, variant, context_chars=200)

                results.append({
                    "snippet": snippet,
                    "file_path": meta.get("file_path", "unknown"),
                    "project": proj_name,
                    "file_type": meta.get("file_type", "unknown"),
                    "matched_variant": variant,
                })

            if len(results) >= top_k:
                break
        if len(results) >= top_k:
            break

    results = results[:top_k]
    log_tool_call("search_hex_pattern", {"pattern": pattern, "project": project}, start_time, len(results))

    if not results:
        return f"No matches found for pattern: '{original_pattern}'"

    output_parts = [f"Found {len(results)} matches for: '{original_pattern}'\n"]
    for i, r in enumerate(results, 1):
        output_parts.append(
            f"--- Match {i} ---\n"
            f"Project: {r['project']}\n"
            f"File: {r['file_path']} ({r['file_type']})\n"
            f"Matched: {r['matched_variant']}\n"
            f"Context:\n{r['snippet']}\n"
        )

    return "\n".join(output_parts)


def find_variable(variable_name: str, project: str = "") -> str:
    """
    Find where a specific variable, constant, enum value, or #define is defined and used.
    Uses text matching to find exact occurrences across indexed files.

    Args:
        variable_name: Name of the variable/constant to find (e.g., "BRU_CASHUNIT_STATUS_FULL", "REROUTE_TARGET_URJB1_INDEX")
        project: Optional project name filter

    Returns:
        Files containing the variable with context snippets
    """
    start_time = time.time()

    if not variable_name or not variable_name.strip():
        return "Error: variable_name is required."

    name = variable_name.strip()

    # Determine which collections to search
    if project:
        collection = _ctx.store.get_collection(project)
        if collection is None:
            return f"Error: Project '{project}' not found."
        collections_to_search = [(project, collection)]
    else:
        collections_meta = _ctx.store.list_collections()
        prefix = _ctx.config.storage.collection_prefix
        collections_to_search = []
        for col_meta in collections_meta:
            try:
                col = _ctx.store.client.get_collection(name=col_meta.name)
                proj_name = col_meta.name.replace(f"{prefix}_", "", 1)
                collections_to_search.append((proj_name, col))
            except Exception:
                continue

    if not collections_to_search:
        return "No indexed collections found."

    results = []
    seen_files = set()

    for proj_name, col in collections_to_search:
        try:
            query_results = col.get(
                where_document={"$contains": name},
                include=["documents", "metadatas"],
            )
        except Exception:
            continue

        if not query_results["ids"]:
            continue

        for i, doc in enumerate(query_results["documents"]):
            meta = query_results["metadatas"][i]
            file_path = meta.get("file_path", "unknown")
            file_key = f"{proj_name}:{file_path}"

            if file_key in seen_files:
                continue
            seen_files.add(file_key)

            snippet = extract_snippet(doc, name, context_chars=150)

            results.append({
                "snippet": snippet,
                "file_path": file_path,
                "project": proj_name,
                "file_type": meta.get("file_type", "unknown"),
            })

            if len(results) >= 15:
                break
        if len(results) >= 15:
            break

    log_tool_call("find_variable", {"variable_name": variable_name, "project": project}, start_time, len(results))

    if not results:
        return f"No occurrences found for: '{name}'"

    output_parts = [f"Found '{name}' in {len(results)} file(s):\n"]
    for i, r in enumerate(results, 1):
        output_parts.append(
            f"--- {i}. {r['file_path']} ({r['file_type']}) ---\n"
            f"Project: {r['project']}\n"
            f"Context:\n{r['snippet']}\n"
        )

    return "\n".join(output_parts)
