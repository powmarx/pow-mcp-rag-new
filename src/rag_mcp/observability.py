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
    level     INFO (tool_call, log_epoch_backfill) | ERROR (embed_failure)
    event     event type discriminator, e.g. "tool_call"
    pid       OS process id (server and indexer are separate processes)
    message   human-readable one-line summary (kept so stderr stays greppable)

``tool_call`` adds:

    tool          tool name, e.g. "search_docs"
    params        the parameters the tool reported (query truncated to 100 chars)
    duration_ms   wall-clock duration, one decimal
    result_count  number of results/chunks the tool reported (always 0 on error)
    outcome       "ok" | "error" — whether the tool returned a result or an error text
    error         only when outcome == "error": the text returned to the client,
                  truncated to 500 chars

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

Counters
--------
Per tool: ``count`` and duration stats cover every call; ``error_calls``
(``outcome == "error"``) and ``zero_result_calls`` (``outcome == "ok"`` and
``result_count == 0``) are disjoint subsets of ``count``.

Every emitted event is also folded into in-process counters (``_Counters``),
exposed by ``get_metrics_snapshot()`` (live, this process only, reset on
restart — what the ``get_metrics`` MCP tool returns). The same counter class
replays the JSONL file via ``aggregate_events_from_log()`` (what the
``rag-mcp get_metrics`` CLI prints), so live and replayed snapshots have an
identical shape by construction; only ``source`` and the ``process`` /
``file`` blocks differ.
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
    _counters.ingest(record)
    line = _serialize(record)
    print(line, file=sys.stderr)
    _write_file_line(line)
    return record


# ---------------------------------------------------------------------------
# Event: tool_call
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


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


TOOL_CALL_OUTCOMES = ("ok", "error")


def log_tool_call_event(
    tool: str,
    params: dict[str, Any],
    start_time: float,
    result_count: int,
    outcome: str = "ok",
    error: str | None = None,
) -> dict[str, Any]:
    """Emit one ``tool_call`` event. ``start_time`` is a ``time.time()`` value.

    ``outcome`` is ``"ok"`` or ``"error"``; anything else falls back to ``"ok"``
    (emitting must never raise). On ``"error"`` the record carries
    ``result_count=0`` and ``error`` (the text returned to the client,
    truncated to ``MAX_ERROR_CHARS``); ``message`` keeps its first line only.
    """
    duration_ms = round((time.time() - start_time) * 1000, 1)
    safe_params = _sanitize_params(params or {})
    if outcome not in TOOL_CALL_OUTCOMES:
        outcome = "ok"
    head = f"{tool}({_format_params(safe_params)})"
    if outcome == "error":
        error_text = _truncate(str(error or ""), MAX_ERROR_CHARS)
        first_line = error_text.splitlines()[0] if error_text else ""
        message = f"{head} -> error in {duration_ms:.0f}ms: {first_line}"
        return emit(
            "tool_call",
            message,
            tool=tool,
            params=safe_params,
            duration_ms=duration_ms,
            result_count=0,
            outcome="error",
            error=error_text,
        )
    message = f"{head} -> {result_count} results in {duration_ms:.0f}ms"
    return emit(
        "tool_call",
        message,
        tool=tool,
        params=safe_params,
        duration_ms=duration_ms,
        result_count=result_count,
        outcome="ok",
    )


# ---------------------------------------------------------------------------
# Event: embed_failure
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

_PROCESS_STARTED_AT = time.time()


class _Counters:
    """Thread-safe aggregate counters over events. Fed by ``emit()`` (live)
    and by ``aggregate_events_from_log()`` (replay) through the same
    ``ingest()`` method, so both paths produce the same numbers for the same
    events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._tool_calls: dict[str, dict[str, Any]] = {}
            self._embed_failures_by_kind: dict[str, int] = {}
            self._embed_failures_by_error_type: dict[str, int] = {}
            self._backfills_by_collection: dict[str, dict[str, int]] = {}

    def ingest(self, record: dict[str, Any]) -> bool:
        """Fold one event record into the counters. Unknown event types are
        ignored (returns False) so old/new log files can be replayed safely."""
        event = record.get("event")
        with self._lock:
            if event == "tool_call":
                tool = str(record.get("tool", "?"))
                duration = float(record.get("duration_ms") or 0.0)
                result_count = record.get("result_count")
                # Records written before ``outcome`` existed (and unknown
                # values from newer writers) replay as "ok".
                is_error = record.get("outcome") == "error"
                t = self._tool_calls.setdefault(
                    tool,
                    {
                        "count": 0,
                        "total_duration_ms": 0.0,
                        "max_duration_ms": 0.0,
                        "error_calls": 0,
                        "zero_result_calls": 0,
                    },
                )
                t["count"] += 1
                t["total_duration_ms"] += duration
                t["max_duration_ms"] = max(t["max_duration_ms"], duration)
                if is_error:
                    t["error_calls"] += 1
                elif result_count == 0:
                    # Only successful calls count as "zero result": an error is
                    # not an empty answer.
                    t["zero_result_calls"] += 1
                return True
            if event == "embed_failure":
                kind = str(record.get("kind", "?"))
                error_type = str(record.get("error_type", "?"))
                self._embed_failures_by_kind[kind] = self._embed_failures_by_kind.get(kind, 0) + 1
                self._embed_failures_by_error_type[error_type] = (
                    self._embed_failures_by_error_type.get(error_type, 0) + 1
                )
                return True
            if event == "log_epoch_backfill":
                collection = str(record.get("collection", "?"))
                c = self._backfills_by_collection.setdefault(collection, {"events": 0, "records_updated": 0})
                c["events"] += 1
                c["records_updated"] += int(record.get("records_updated") or 0)
                return True
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_tool = {}
            for tool in sorted(self._tool_calls):
                t = self._tool_calls[tool]
                by_tool[tool] = {
                    "count": t["count"],
                    "total_duration_ms": round(t["total_duration_ms"], 1),
                    "avg_duration_ms": round(t["total_duration_ms"] / t["count"], 1),
                    "max_duration_ms": round(t["max_duration_ms"], 1),
                    "error_calls": t["error_calls"],
                    "zero_result_calls": t["zero_result_calls"],
                }
            by_collection = {c: dict(v) for c, v in sorted(self._backfills_by_collection.items())}
            return {
                "tool_calls": {
                    "total": sum(t["count"] for t in by_tool.values()),
                    "errors": sum(t["error_calls"] for t in by_tool.values()),
                    "by_tool": by_tool,
                },
                "embed_failures": {
                    "total": sum(self._embed_failures_by_kind.values()),
                    "by_kind": dict(sorted(self._embed_failures_by_kind.items())),
                    "by_error_type": dict(sorted(self._embed_failures_by_error_type.items())),
                },
                "log_epoch_backfills": {
                    # Only backfills that updated something emit an event, so
                    # this is "backfills with updates", not "checks".
                    "events": sum(c["events"] for c in by_collection.values()),
                    "records_updated_total": sum(c["records_updated"] for c in by_collection.values()),
                    "by_collection": by_collection,
                },
            }


_counters = _Counters()


def reset_counters() -> None:
    """Zero the live counters (tests)."""
    _counters.reset()


def get_metrics_snapshot() -> dict[str, Any]:
    """Live counters of THIS process (what the ``get_metrics`` MCP tool returns)."""
    return {
        "source": "live",
        "process": {
            "pid": os.getpid(),
            "started_at": datetime.fromtimestamp(_PROCESS_STARTED_AT, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
        },
        **_counters.snapshot(),
    }


# ---------------------------------------------------------------------------
# JSONL replay (CLI)
# ---------------------------------------------------------------------------


def aggregate_events_from_log(path: str | Path) -> dict[str, Any]:
    """Replay a JSONL events file into the same snapshot shape as
    ``get_metrics_snapshot()``. Malformed or truncated lines (e.g. a crash
    mid-write) and unknown event types are skipped and counted, never fatal.
    A missing file yields an all-zero snapshot with ``file.exists == False``."""
    path = Path(path)
    counters = _Counters()
    lines_total = lines_parsed = lines_skipped = 0
    first_ts: str | None = None
    last_ts: str | None = None
    pids: set[int] = set()

    if path.exists():
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                lines_total += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    lines_skipped += 1
                    continue
                if not isinstance(record, dict) or not counters.ingest(record):
                    lines_skipped += 1
                    continue
                lines_parsed += 1
                ts = record.get("ts")
                if isinstance(ts, str):
                    first_ts = first_ts or ts
                    last_ts = ts
                if isinstance(record.get("pid"), int):
                    pids.add(record["pid"])

    return {
        "source": "jsonl_replay",
        "file": {
            "path": str(path),
            "exists": path.exists(),
            "lines_total": lines_total,
            "lines_parsed": lines_parsed,
            "lines_skipped": lines_skipped,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "process_count": len(pids),
        },
        **counters.snapshot(),
    }


def format_metrics_text(snapshot: dict[str, Any]) -> str:
    """Human-readable rendering of a live or replayed snapshot."""
    out: list[str] = []
    if snapshot.get("source") == "jsonl_replay":
        f = snapshot["file"]
        out.append(f"Event log : {f['path']}")
        if not f["exists"]:
            out.append("            (file not found — nothing has been logged yet)")
        else:
            span = f"{f['first_ts']} -> {f['last_ts']}" if f["first_ts"] else "-"
            out.append(
                f"Events    : {f['lines_parsed']} parsed, {f['lines_skipped']} skipped "
                f"(malformed/unknown), {f['process_count']} process(es)"
            )
            out.append(f"Span      : {span}")
    else:
        p = snapshot["process"]
        out.append(f"Process   : pid {p['pid']}, started {p['started_at']}, up {p['uptime_seconds']}s (live counters)")

    tc = snapshot["tool_calls"]
    out.append("")
    errors = tc.get("errors", 0)
    out.append(f"Tool calls: {tc['total']}" + (f" ({errors} error{'s' if errors != 1 else ''})" if errors else ""))
    if tc["by_tool"]:
        out.append(
            f"  {'tool':<24} {'calls':>6} {'errors':>6} {'avg ms':>9} {'max ms':>9} {'zero-result':>12}"
        )
        for tool, t in tc["by_tool"].items():
            out.append(
                f"  {tool:<24} {t['count']:>6} {t.get('error_calls', 0):>6} {t['avg_duration_ms']:>9.1f} "
                f"{t['max_duration_ms']:>9.1f} {t['zero_result_calls']:>12}"
            )

    ef = snapshot["embed_failures"]
    out.append("")
    out.append(f"Embed failures: {ef['total']}")
    if ef["total"]:
        out.append("  by kind      : " + ", ".join(f"{k}={v}" for k, v in ef["by_kind"].items()))
        out.append("  by error type: " + ", ".join(f"{k}={v}" for k, v in ef["by_error_type"].items()))

    bf = snapshot["log_epoch_backfills"]
    out.append("")
    out.append(
        f"Log-epoch backfills: {bf['events']} event(s), {bf['records_updated_total']} record(s) updated"
    )
    for collection, c in bf["by_collection"].items():
        out.append(f"  {collection:<24} {c['events']} event(s), {c['records_updated']} updated")
    return "\n".join(out)
