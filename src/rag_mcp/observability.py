"""
Structured (JSON) event logging for the RAG MCP server.

Every event is one JSON object, emitted as a single line to two sinks:

  1. stderr  — always. MCP clients (Kiro, VS Code) capture the server's stderr,
     so this replaces the old free-text ``[tool] ...`` lines.
  2. A JSONL file — only when a path is known. Resolved, highest priority first:
       a. ``RAG_OBSERVABILITY_LOG_PATH`` env var (explicit override)
       b. the path passed to ``configure()`` by the server at startup
          (``<storage.path>/observability/events.jsonl``)
       c. nothing -> file sink disabled, stderr only (e.g. unit tests that
          never boot the server must not write into the developer's home).

Design constraints:
  * stdlib only (no Prometheus, no daemon, no extra dependencies).
  * Emitting an event must never break the caller: file-sink failures are
    reported once on stderr and then ignored.
  * The file is opened in append mode per write (no long-lived handle), so a
    line is one ``write()`` call; a process-wide lock serialises writers
    inside this process.

Event envelope (common to all event types):

    ts        ISO 8601 UTC with millisecond precision, e.g. 2026-04-28T05:10:00.123Z
    level     INFO | WARNING | ERROR
    event     event type discriminator, e.g. "tool_call"
    pid       OS process id (server and indexer are separate processes)
    message   human-readable one-line summary (kept so stderr stays greppable)

``tool_call`` adds:

    tool          tool name, e.g. "search_docs"
    params        the parameters the tool reported (query truncated to 100 chars)
    duration_ms   wall-clock duration, one decimal
    result_count  number of results/chunks the tool reported

``embed_failure`` (level ERROR) adds — deliberately no file/project context,
it is emitted from inside ``EmbeddingGenerator`` which knows nothing about
what is being indexed:

    kind          "documents" (indexing / log indexing) | "query" (search time)
    batch_size    number of texts in the failed batch
    model         embedding model name
    error_type    exception class name, e.g. "RuntimeError"
    error         exception message, truncated to 500 chars

``log_epoch_backfill`` adds (emitted by ``ChromaStore.backfill_log_epochs``
only when at least one record was updated; a no-op check emits nothing):

    collection       ChromaDB collection name, e.g. "rag_logs"
    records_checked  log chunks in the collection
    records_missing  chunks that had no numeric epoch fields
    records_updated  chunks that received them (missing minus unparseable)
    duration_ms      wall-clock duration of the whole backfill, one decimal
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_LOG_PATH = "RAG_OBSERVABILITY_LOG_PATH"
DEFAULT_LOG_RELATIVE_PATH = Path("observability") / "events.jsonl"

# Queries are user-supplied free text; cap what we persist (same limit the old
# free-text line used).
MAX_QUERY_CHARS = 100
# Exception messages can embed whole inputs (e.g. tokenizer errors); cap them.
MAX_ERROR_CHARS = 500

_configured_path: Path | None = None
_file_lock = threading.Lock()
_file_sink_warned = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configure(log_path: str | Path | None) -> None:
    """Set (or clear, with ``None``) the JSONL file sink for this process.

    Idempotent. ``RAG_OBSERVABILITY_LOG_PATH`` still wins if set.
    """
    global _configured_path, _file_sink_warned
    _configured_path = Path(log_path) if log_path else None
    _file_sink_warned = False


def default_log_path(storage_path: str | Path) -> Path:
    """Conventional file location: next to the ChromaDB data."""
    return Path(storage_path) / DEFAULT_LOG_RELATIVE_PATH


def resolve_log_path() -> Path | None:
    """Effective JSONL path, or None when the file sink is disabled."""
    env = os.environ.get(ENV_LOG_PATH, "").strip()
    if env:
        return Path(env)
    return _configured_path


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _serialize(record: dict[str, Any]) -> str:
    # default=str: never let an odd param type (Path, Enum, ...) break a tool call.
    return json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))


def _write_file_line(line: str) -> None:
    global _file_sink_warned
    path = resolve_log_path()
    if path is None:
        return
    try:
        with _file_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:
        if not _file_sink_warned:
            _file_sink_warned = True
            print(
                f"[observability] WARNING: cannot append to {path} ({e}); "
                "file sink disabled for this process, stderr only",
                file=sys.stderr,
            )


def emit(event: str, message: str, level: str = "INFO", **fields: Any) -> dict[str, Any]:
    """Build the envelope, write one JSON line to stderr and (if enabled) to
    the JSONL file. Returns the record (handy for tests)."""
    record: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "level": level,
        "event": event,
        "pid": os.getpid(),
        "message": message,
    }
    record.update(fields)
    line = _serialize(record)
    print(line, file=sys.stderr)
    _write_file_line(line)
    return record


# ---------------------------------------------------------------------------
# Event: tool_call
# ---------------------------------------------------------------------------


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key == "query" and isinstance(value, str) and len(value) > MAX_QUERY_CHARS:
            value = value[:MAX_QUERY_CHARS] + "..."
        out[key] = value
    return out


def _format_params(params: dict[str, Any]) -> str:
    """Same rendering as the old free-text line: query first, then the rest."""
    rest = ", ".join(f"{k}={v!r}" for k, v in params.items() if k != "query")
    query = params.get("query")
    if query:
        return f"query={query!r}" + (f", {rest}" if rest else "")
    return rest


def log_tool_call_event(
    tool: str,
    params: dict[str, Any],
    start_time: float,
    result_count: int,
) -> dict[str, Any]:
    """Emit one ``tool_call`` event. ``start_time`` is a ``time.time()`` value."""
    duration_ms = round((time.time() - start_time) * 1000, 1)
    safe_params = _sanitize_params(params or {})
    message = f"{tool}({_format_params(safe_params)}) -> {result_count} results in {duration_ms:.0f}ms"
    return emit(
        "tool_call",
        message,
        tool=tool,
        params=safe_params,
        duration_ms=duration_ms,
        result_count=result_count,
    )


# ---------------------------------------------------------------------------
# Event: embed_failure
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def log_embed_failure_event(
    error: BaseException,
    kind: str,
    batch_size: int,
    model: str,
) -> dict[str, Any]:
    """Emit one ``embed_failure`` event (level ERROR). Never raises."""
    error_type = type(error).__name__
    error_text = _truncate(str(error), MAX_ERROR_CHARS)
    message = (
        f"embedding failed (kind={kind}, batch_size={batch_size}, model={model}): "
        f"{error_type}: {error_text}"
    )
    return emit(
        "embed_failure",
        message,
        level="ERROR",
        kind=kind,
        batch_size=batch_size,
        model=model,
        error_type=error_type,
        error=error_text,
    )


# ---------------------------------------------------------------------------
# Event: log_epoch_backfill
# ---------------------------------------------------------------------------


def log_log_epoch_backfill_event(
    collection: str,
    records_checked: int,
    records_missing: int,
    records_updated: int,
    start_time: float,
) -> dict[str, Any]:
    """Emit one ``log_epoch_backfill`` event. ``start_time`` is a ``time.time()`` value."""
    duration_ms = round((time.time() - start_time) * 1000, 1)
    message = (
        f"backfilled epoch timestamps on {records_updated}/{records_missing} legacy log chunks "
        f"in {collection} ({records_checked} checked) in {duration_ms:.0f}ms"
    )
    return emit(
        "log_epoch_backfill",
        message,
        collection=collection,
        records_checked=records_checked,
        records_missing=records_missing,
        records_updated=records_updated,
        duration_ms=duration_ms,
    )
