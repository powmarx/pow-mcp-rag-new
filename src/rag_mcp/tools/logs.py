"""Log tools: search_logs, cancel_indexing, index_log_file."""

import sys
import time
from pathlib import Path

import anyio

from rag_mcp.tools import ToolContext
from rag_mcp.tools.helpers import log_tool_call

_ctx: ToolContext | None = None

# --- Allowed severity levels for search_logs ---
_VALID_SEVERITIES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def register(mcp, ctx: ToolContext) -> None:
    """Register log tools on the MCP server."""
    global _ctx
    _ctx = ctx

    mcp.tool()(search_logs)
    mcp.tool()(cancel_indexing)
    mcp.tool()(index_log_file)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_log_where_filter(
    severity: str,
    device_id: str,
    event_type: str,
    time_range_start: str,
    time_range_end: str,
) -> dict:
    """Build ChromaDB where clause from log filter parameters.

    Always includes file_type == "log" to restrict results to log chunks.
    """
    conditions = [{"file_type": "log"}]

    if severity:
        conditions.append({"severity": severity.lower()})
    if device_id:
        conditions.append({"device_id": device_id})
    if event_type:
        conditions.append({"event_type": event_type})

    # Time range filtering uses string comparison (ISO 8601 is lexicographically sortable)
    if time_range_start:
        conditions.append({"timestamp_range_start": {"$gte": time_range_start}})
    if time_range_end:
        conditions.append({"timestamp_range_end": {"$lte": time_range_end}})

    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _format_log_results(results_list: list[dict]) -> str:
    """Format search_logs results into a human-readable string."""
    if not results_list:
        return "Found 0 log results.\n\nNo matching log events found."

    output_parts = [f"Found {len(results_list)} log result(s).\n"]
    for i, r in enumerate(results_list, 1):
        output_parts.append(
            f"--- Result {i} (score: {r['score']:.4f}) ---\n"
            f"File: {r['file_path']}\n"
            f"Project: {r.get('project', '')}\n"
            f"Severity: {r.get('severity', '')}\n"
            f"Event Type: {r.get('event_type', '')}\n"
            f"Device ID: {r.get('device_id', '')}\n"
            f"Error Code: {r.get('error_code', '')}\n"
            f"Time Range: {r.get('timestamp_range_start', '')} — {r.get('timestamp_range_end', '')}\n"
            f"Lines: {r.get('line_start', '')}–{r.get('line_end', '')}\n"
            f"Content:\n{r['content']}\n"
        )

    return "\n".join(output_parts)


def _process_log_batch(
    batch_lines: list[str],
    log_parser,
    content_transform,
    event_grouper,
    embedding_gen_ref,
    collection,
    relative_path: str,
    source,
    project: str,
    parsed_from: str | None,
    parsed_to: str | None,
    global_chunk_offset: int,
) -> tuple[int, int]:
    """Process a batch of log lines through the full pipeline: parse → filter → transform → group → embed → store.

    Returns:
        Tuple of (chunks_stored, events_count) for this batch.
    """
    batch_content = "\n".join(batch_lines)
    events = log_parser.parse(batch_content)

    if not events:
        return 0, 0

    # Apply time-range filter
    if parsed_from or parsed_to:
        filtered = []
        for ev in events:
            ts = ev.timestamp_iso
            if "T" in ts:
                time_part = ts.split("T")[1][:12]
            else:
                time_part = ts[:12]
            if parsed_from and time_part < parsed_from:
                continue
            if parsed_to and time_part > parsed_to:
                continue
            filtered.append(ev)
        events = filtered

    if not events:
        return 0, 0

    events_count = len(events)

    # Apply content transforms
    for ev in events:
        full_text = ev.message
        if ev.continuation_lines:
            full_text += "\n" + "\n".join(ev.continuation_lines)
        ev.message = content_transform.transform(full_text)
        ev.continuation_lines = []

    # Group events
    groups = event_grouper.group(events)

    if not groups:
        return 0, events_count

    # Generate embeddings
    texts = [g.text for g in groups]
    embeddings = embedding_gen_ref.encode(texts)

    # Build metadata and store
    ids = []
    metadatas = []
    for idx, group in enumerate(groups):
        # Use line_start as part of ID for deterministic, resumable indexing
        chunk_id = f"{relative_path}::line_{group.line_start}"
        ids.append(chunk_id)
        metadatas.append({
            "file_path": relative_path,
            "file_type": "log",
            "project": project,
            "source_description": source.description,
            "chunk_index": global_chunk_offset + idx,
            "total_chunks": 0,  # Unknown until file is fully processed
            "event_type": group.event_type or "",
            "severity": group.severity or "",
            "device_id": group.device_id or "",
            "error_code": group.error_code or "",
            "timestamp_range_start": group.timestamp_start or "",
            "timestamp_range_end": group.timestamp_end or "",
            "line_start": group.line_start,
            "line_end": group.line_end,
            "record_type": "log_event",
        })

    # Upsert in sub-batches of 5000 (ChromaDB limit)
    for start in range(0, len(ids), 5000):
        end = start + 5000
        collection.upsert(
            ids=ids[start:end],
            embeddings=embeddings[start:end],
            documents=texts[start:end],
            metadatas=metadatas[start:end],
        )

    return len(groups), events_count


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def search_logs(
    query: str = "",
    project: str = "",
    severity: str = "",
    time_range_start: str = "",
    time_range_end: str = "",
    error_code_pattern: str = "",
    device_id: str = "",
    event_type: str = "",
    top_k: int = 20,
) -> str:
    """
    Search indexed log events with structured filtering and semantic search.

    Args:
        query: Semantic search query (max 512 chars). Optional if filters provided.
        project: Filter by project name.
        severity: Filter by severity (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        time_range_start: ISO 8601 start time filter.
        time_range_end: ISO 8601 end time filter.
        error_code_pattern: Prefix match on error codes (e.g., "88a1").
        device_id: Filter by device identifier.
        event_type: Filter by event type (command, response, error, etc.).
        top_k: Max results (default 20, max 50). Clamped to range [1, 50].

    Returns:
        Formatted results with metadata and relevance scores.
    """
    return await anyio.to_thread.run_sync(lambda: _search_logs_sync(
        query, project, severity, time_range_start,
        time_range_end, error_code_pattern, device_id, event_type, top_k,
    ))


def _search_logs_sync(
    query: str = "",
    project: str = "",
    severity: str = "",
    time_range_start: str = "",
    time_range_end: str = "",
    error_code_pattern: str = "",
    device_id: str = "",
    event_type: str = "",
    top_k: int = 20,
) -> str:
    """Synchronous implementation of search_logs (runs in thread pool)."""
    start_time = time.time()

    # --- Parameter validation ---

    # Validate severity
    if severity and severity.upper() not in _VALID_SEVERITIES:
        return (
            f"Error: Invalid severity '{severity}'. "
            f"Must be one of: {', '.join(sorted(_VALID_SEVERITIES))}."
        )

    # Validate that at least one search criterion is provided
    has_query = bool(query and query.strip())
    has_filter = bool(
        severity or time_range_start or time_range_end
        or error_code_pattern or device_id or event_type or project
    )
    if not has_query and not has_filter:
        return "Error: At least one of 'query' or a filter parameter must be provided."

    # Validate query length
    if query and len(query) > 512:
        return "Error: query must not exceed 512 characters."

    # Validate error_code_pattern length
    if error_code_pattern and len(error_code_pattern) > 32:
        return "Error: error_code_pattern must not exceed 32 characters."

    # Clamp top_k to [1, 50]
    top_k = max(1, min(top_k, 50))

    # --- Determine collections to search ---
    if project:
        collection = _ctx.store.get_collection(project)
        if collection is None:
            return f"Error: Project '{project}' not found. Use list_projects() to see available projects."
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
        return "No indexed collections found. Run 'python indexer.py' first."

    # --- Build where clause ---
    where_filter = _build_log_where_filter(
        severity=severity,
        device_id=device_id,
        event_type=event_type,
        time_range_start=time_range_start,
        time_range_end=time_range_end,
    )

    # Over-fetch when error_code_pattern is used (post-filter step)
    fetch_count = top_k * 3 if error_code_pattern else top_k

    # --- Execute query ---
    all_results = []

    for proj_name, col in collections_to_search:
        try:
            if has_query:
                # Semantic search path: embed query and use collection.query
                _ctx.ensure_model_loaded()
                query_embedding = _ctx.embedding_gen.encode_query(query.strip())

                query_results = col.query(
                    query_embeddings=[query_embedding],
                    n_results=fetch_count,
                    where=where_filter,
                    include=["documents", "metadatas", "distances"],
                )

                if query_results["documents"] and query_results["documents"][0]:
                    for i, doc in enumerate(query_results["documents"][0]):
                        meta = query_results["metadatas"][0][i]
                        distance = query_results["distances"][0][i]
                        # Convert distance to relevance score [0.0, 1.0]
                        score = max(0.0, min(1.0, 1.0 - distance))
                        all_results.append({
                            "content": doc,
                            "file_path": meta.get("file_path", "unknown"),
                            "project": meta.get("project", proj_name),
                            "severity": meta.get("severity", ""),
                            "event_type": meta.get("event_type", ""),
                            "device_id": meta.get("device_id", ""),
                            "error_code": meta.get("error_code", ""),
                            "timestamp_range_start": meta.get("timestamp_range_start", ""),
                            "timestamp_range_end": meta.get("timestamp_range_end", ""),
                            "line_start": meta.get("line_start", ""),
                            "line_end": meta.get("line_end", ""),
                            "score": score,
                        })
            else:
                # Filter-only path: use collection.get
                get_results = col.get(
                    where=where_filter,
                    limit=fetch_count,
                    include=["documents", "metadatas"],
                )

                if get_results["ids"]:
                    for i, doc in enumerate(get_results["documents"]):
                        meta = get_results["metadatas"][i]
                        all_results.append({
                            "content": doc,
                            "file_path": meta.get("file_path", "unknown"),
                            "project": meta.get("project", proj_name),
                            "severity": meta.get("severity", ""),
                            "event_type": meta.get("event_type", ""),
                            "device_id": meta.get("device_id", ""),
                            "error_code": meta.get("error_code", ""),
                            "timestamp_range_start": meta.get("timestamp_range_start", ""),
                            "timestamp_range_end": meta.get("timestamp_range_end", ""),
                            "line_start": meta.get("line_start", ""),
                            "line_end": meta.get("line_end", ""),
                            "score": 1.0,  # No distance metric for filter-only
                        })
        except Exception as e:
            print(f"[search_logs] Error querying collection {proj_name}: {e}", file=sys.stderr)
            continue

    # --- Post-filter for error_code_pattern (prefix match) ---
    if error_code_pattern:
        all_results = [
            r for r in all_results
            if r.get("error_code", "").startswith(error_code_pattern)
        ]

    # --- Sort and trim ---
    if has_query:
        # Sort by relevance score descending
        all_results.sort(key=lambda x: x["score"], reverse=True)
    else:
        # Sort by timestamp_range_end descending for filter-only queries
        all_results.sort(key=lambda x: x.get("timestamp_range_end", ""), reverse=True)

    all_results = all_results[:top_k]

    log_tool_call(
        "search_logs",
        {"query": query, "project": project, "severity": severity, "top_k": top_k},
        start_time,
        len(all_results),
    )

    return _format_log_results(all_results)


def cancel_indexing() -> str:
    """
    Cancel any in-progress background reindex operation.
    Chunks already stored are preserved. You can resume later by calling index_log_file again.

    NOTE: This does NOT stop a running index_log_file call (MCP tools are synchronous).
    To stop index_log_file, restart the MCP server. Stored chunks are preserved.

    Returns:
        Confirmation that cancellation was requested.
    """
    _ctx.set_indexing_cancelled(True)
    print("[cancel_indexing] Cancellation requested", file=sys.stderr)
    return (
        "Cancellation requested.\n\n"
        "• Background reindex: will stop after current file.\n"
        "• index_log_file: NOT affected (MCP tools are synchronous).\n"
        "  To stop it, restart the MCP server from Kiro's MCP panel.\n\n"
        "Already-stored chunks are preserved (deterministic IDs)."
    )


async def index_log_file(
    file: str,
    project: str = "",
    time_from: str = "",
    time_to: str = "",
) -> str:
    """
    Index a specific log file (or time window within it) on demand.
    Use this instead of waiting for background reindex on large log files.

    Args:
        file: Log filename or glob pattern (e.g., "device-26-04-28.log", "device-2026-05-14*.log")
        project: Project name containing log sources (required)
        time_from: Optional start time filter, HH:MM:SS format (e.g., "14:00:00"). Only index events at or after this time.
        time_to: Optional end time filter, HH:MM:SS format (e.g., "15:30:00"). Only index events at or before this time.

    Returns:
        Summary of indexing results (files processed, chunks created, time range)
    """
    return await anyio.to_thread.run_sync(lambda: _index_log_file_sync(file, project, time_from, time_to))


def _index_log_file_sync(
    file: str,
    project: str = "",
    time_from: str = "",
    time_to: str = "",
) -> str:
    """Synchronous implementation of index_log_file (runs in thread pool)."""
    import glob as _glob

    start_time = time.time()

    if not project or not project.strip():
        return "Error: project is required."

    # Reset cancellation flag
    _ctx.set_indexing_cancelled(False)

    # Validate project exists
    project_config = None
    for p in _ctx.config.projects:
        if p.name == project:
            project_config = p
            break

    if project_config is None:
        return f"Error: Project '{project}' not found. Use list_projects() to see available projects."

    if project_config.removed:
        return f"Error: Project '{project}' is marked as removed. Re-add it first with add_project()."

    # Find log sources in the project
    log_sources = [s for s in project_config.sources if s.type == "log"]
    if not log_sources:
        return f"Error: Project '{project}' has no log-type sources configured."

    # Validate time format
    def _parse_time(t: str) -> str | None:
        """Parse HH:MM:SS or HH:MM:SS:mmm into comparable ISO time string."""
        if not t:
            return None
        t = t.strip()
        # Support HH:MM:SS and HH:MM:SS:mmm (device log format)
        parts = t.split(":")
        if len(parts) < 3:
            return None
        try:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            ms = int(parts[3]) if len(parts) > 3 else 0
            # Return as sortable string (ISO-compatible time portion)
            return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
        except (ValueError, IndexError):
            return None

    parsed_from = _parse_time(time_from) if time_from else None
    parsed_to = _parse_time(time_to) if time_to else None

    if time_from and parsed_from is None:
        return f"Error: Invalid time_from format '{time_from}'. Use HH:MM:SS (e.g., '14:00:00')."
    if time_to and parsed_to is None:
        return f"Error: Invalid time_to format '{time_to}'. Use HH:MM:SS (e.g., '15:30:00')."

    # Lazy load model
    _ctx.ensure_model_loaded()

    # Find matching files
    base_path = Path(project_config.base_path)
    matched_files = []
    for source in log_sources:
        pattern = str(base_path / source.pattern)
        all_log_files = _glob.glob(pattern, recursive=True)
        for f in all_log_files:
            fp = Path(f)
            if fp.is_file() and (file in fp.name or fp.match(file)):
                matched_files.append((fp, source))

    if not matched_files:
        return f"Error: No log files matching '{file}' found in project '{project}' (base: {base_path})."

    # Import pipeline components
    from rag_mcp.log.parsing.config_models import LogSettings
    from rag_mcp.log.parsing.content_transform import ContentTransform
    from rag_mcp.log.parsing.event_grouper import EventGrouper
    from rag_mcp.log.parsing.line_filter import LineFilter
    from rag_mcp.log.parsing.log_parser import LogParser

    settings = project_config.log_settings if project_config.log_settings else LogSettings()

    total_chunks = 0
    results_summary = []

    for filepath, source in matched_files:
        if _ctx.indexing_cancelled():
            results_summary.append(f"  ⚠ Cancelled before processing {filepath.name}")
            break

        file_start = time.time()
        log_patterns = source.log_patterns if source.log_patterns else None

        # Build pipeline components
        line_filter = LineFilter(
            filters=settings.line_filters,
            default_action=settings.default_filter_action,
        )
        content_transform = ContentTransform(transforms=settings.content_transforms)

        relative_path = str(filepath.relative_to(base_path)).replace("\\", "/")

        log_parser = LogParser(
            patterns=log_patterns,
            settings=settings,
            severity_mapping=settings.severity_mapping or None,
            line_filter=line_filter,
            filename=relative_path,
        )
        event_grouper = EventGrouper(
            settings=settings,
            grouping_rules=settings.grouping_rules or None,
        )

        # Stream the full pipeline in batches
        file_size_mb = filepath.stat().st_size / (1024 * 1024)
        print(
            f"[index_log_file] Processing {filepath.name} ({file_size_mb:.1f}MB)...",
            file=sys.stderr,
        )

        collection = _ctx.store.get_or_create_collection(project, project_config.description)

        BATCH_LINES = 10_000  # Parse 10k lines at a time
        file_size_bytes = filepath.stat().st_size
        estimated_total_lines = file_size_bytes // 65  # ~65 bytes per line average
        file_chunks = 0
        file_events = 0
        batch_num = 0
        global_chunk_idx = 0

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                batch_lines = []
                cancelled = False
                past_time_window = False

                for line in fh:
                    # Fast early-stop: if time_to is set and we've passed it, stop reading
                    if parsed_to and not past_time_window:
                        if len(line) > 15 and line[3] == ':' and line[6] == ':':
                            raw_ts = line[4:16]  # "HH:MM:SS:mmm"
                            parts = raw_ts.split(":")
                            if len(parts) >= 3:
                                try:
                                    quick_time = f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}.000"
                                    if quick_time > parsed_to:
                                        past_time_window = True
                                except (ValueError, IndexError):
                                    pass

                    if past_time_window:
                        break

                    batch_lines.append(line.rstrip("\n\r"))
                    if len(batch_lines) >= BATCH_LINES:
                        # Check cancellation before processing next batch
                        if _ctx.indexing_cancelled():
                            cancelled = True
                            break

                        batch_num += 1
                        result = _process_log_batch(
                            batch_lines, log_parser, content_transform, event_grouper,
                            _ctx.embedding_gen, collection, relative_path, source,
                            project, parsed_from, parsed_to, global_chunk_idx,
                        )
                        file_chunks += result[0]
                        file_events += result[1]
                        global_chunk_idx += result[0]
                        batch_lines = []
                        # Log progress every 10 batches (every ~100k lines)
                        if batch_num % 10 == 0 or batch_num == 1:
                            lines_processed = batch_num * BATCH_LINES
                            pct = min(100, lines_processed * 100 // max(estimated_total_lines, 1))
                            print(
                                f"[index_log_file] {filepath.name}: "
                                f"{pct}% ({file_chunks} chunks, {file_events} events)",
                                file=sys.stderr,
                            )

                # Process remaining lines (unless cancelled)
                if batch_lines and not _ctx.indexing_cancelled():
                    result = _process_log_batch(
                        batch_lines, log_parser, content_transform, event_grouper,
                        _ctx.embedding_gen, collection, relative_path, source,
                        project, parsed_from, parsed_to, global_chunk_idx,
                    )
                    file_chunks += result[0]
                    file_events += result[1]

                if cancelled or _ctx.indexing_cancelled():
                    results_summary.append(
                        f"  ⚠ {filepath.name}: cancelled after batch {batch_num} "
                        f"({file_chunks} chunks stored, preserved)"
                    )
                    print(
                        f"[index_log_file] {filepath.name}: CANCELLED after {file_chunks} chunks",
                        file=sys.stderr,
                    )
                    break  # Stop processing more files

        except Exception as e:
            results_summary.append(f"  ✗ {filepath.name}: error - {e}")
            continue

        if file_chunks == 0:
            results_summary.append(f"  ✗ {filepath.name}: no indexable events")
            continue

        total_chunks += file_chunks
        file_ms = (time.time() - file_start) * 1000

        time_info = ""
        if parsed_from or parsed_to:
            time_info = f" (time: {time_from or '*'} → {time_to or '*'})"

        results_summary.append(
            f"  ✓ {filepath.name}: {file_events} events → {file_chunks} chunks "
            f"in {file_ms:.0f}ms{time_info}"
        )
        print(
            f"[index_log_file] {filepath.name}: done - {file_chunks} chunks in {file_ms:.0f}ms",
            file=sys.stderr,
        )

    elapsed_ms = (time.time() - start_time) * 1000

    log_tool_call(
        "index_log_file",
        {"file": file, "project": project, "time_from": time_from, "time_to": time_to},
        start_time,
        total_chunks,
    )

    # Format output
    output_parts = [
        f"Log indexing complete: {total_chunks} chunks created in {elapsed_ms:.0f}ms\n",
        f"Project: {project}",
        f"File pattern: {file}",
    ]
    if time_from or time_to:
        output_parts.append(f"Time range: {time_from or '*'} → {time_to or '*'}")
    output_parts.append(f"\nResults ({len(matched_files)} file(s)):")
    output_parts.extend(results_summary)

    return "\n".join(output_parts)
