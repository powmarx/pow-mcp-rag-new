"""Tests for search_logs time-range filtering.

Regression coverage for the bug where ``$gte``/``$lte`` were applied to ISO
*string* metadata (ChromaDB only supports numeric range operators), the
resulting ValueError was swallowed, and every time-filtered search silently
returned zero results.

These tests run against a real in-memory ChromaDB (EphemeralClient) on the
filter-only path, so no embedding model is required. They live outside
``tests/mcp_tools`` on purpose: that package's conftest boots the full server
(and loads the embedding model) at collection time, which is not needed here.
``rag_mcp.tools.logs`` is imported directly and wired to a minimal ToolContext.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import chromadb
import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.chroma_store import ChromaStore
from rag_mcp.config_loader import StorageConfig
from rag_mcp.log.timestamps import (
    TS_END_EPOCH_KEY,
    TS_START_EPOCH_KEY,
    epoch_fields,
    iso_to_epoch,
    parse_time_filter,
)
from rag_mcp.tools import logs as logs_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PREFIX = "rag"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _log_meta(start_iso: str, end_iso: str, severity="error", with_epoch=True, **extra):
    meta = {
        "file_path": "device.log",
        "file_type": "log",
        "project": "logs",
        "source_description": "",
        "chunk_index": 0,
        "total_chunks": 3,
        "event_type": "error",
        "severity": severity,
        "device_id": "dev1",
        "error_code": "",
        "timestamp_range_start": start_iso,
        "timestamp_range_end": end_iso,
        "line_start": 1,
        "line_end": 2,
        "record_type": "log_event",
    }
    if with_epoch:
        meta.update(epoch_fields(start_iso, end_iso))
    meta.update(extra)
    return meta


@pytest.fixture
def store():
    """A ChromaStore backed by an in-memory client."""
    s = ChromaStore(StorageConfig(path="unused", collection_prefix=PREFIX))
    client = chromadb.EphemeralClient()
    # EphemeralClient is a process-wide singleton: start every test clean.
    for c in client.list_collections():
        client.delete_collection(c.name)
    s.client = client
    return s


@pytest.fixture
def ctx(store, monkeypatch):
    """Minimal ToolContext wired into rag_mcp.tools.logs."""
    c = MagicMock()
    c.store = store
    c.config.storage.collection_prefix = PREFIX
    c.ensure_model_loaded = MagicMock()
    monkeypatch.setattr(logs_mod, "_ctx", c)
    monkeypatch.setattr(logs_mod, "_epoch_backfilled_collections", set())
    return c


def _add_three_events(collection, with_epoch=True):
    """before / inside / straddling the window 05:00–06:00 on 2026-04-28."""
    collection.add(
        ids=["before", "inside", "straddle"],
        embeddings=[[0.1, 0.2, 0.3]] * 3,
        documents=["event before window", "event inside window", "event straddling window end"],
        metadatas=[
            _log_meta("2026-04-28T03:00:00Z", "2026-04-28T03:05:00Z", with_epoch=with_epoch),
            _log_meta("2026-04-28T05:10:00Z", "2026-04-28T05:20:00Z", with_epoch=with_epoch),
            _log_meta("2026-04-28T05:55:00Z", "2026-04-28T06:10:00Z", with_epoch=with_epoch),
        ],
    )


WINDOW_START = "2026-04-28T05:00:00Z"
WINDOW_END = "2026-04-28T06:00:00Z"


# ---------------------------------------------------------------------------
# Unit: timestamp helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iso, expected",
    [
        ("2026-04-28T05:00:00Z", _epoch("2026-04-28T05:00:00Z")),
        ("2026-04-28T05:00:00+00:00", _epoch("2026-04-28T05:00:00Z")),
        ("2026-04-28T08:00:00+03:00", _epoch("2026-04-28T05:00:00Z")),
        ("2026-04-28T05:00:00.500Z", _epoch("2026-04-28T05:00:00Z") + 0.5),
        ("2026-04-28 05:00:00", _epoch("2026-04-28T05:00:00Z")),  # naive -> UTC
        ("2026-04-28", _epoch("2026-04-28T00:00:00Z")),
    ],
)
def test_iso_to_epoch_accepts_common_forms(iso, expected):
    assert iso_to_epoch(iso) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", None, "   ", "28/04/2026", "yesterday", "2026-13-45T00:00:00Z"])
def test_iso_to_epoch_returns_none_for_bad_input(bad):
    assert iso_to_epoch(bad) is None


def test_epoch_fields_omits_unparseable_side():
    assert epoch_fields("", "") == {}
    only_end = epoch_fields("", "2026-04-28T05:00:00Z")
    assert TS_START_EPOCH_KEY not in only_end
    assert only_end[TS_END_EPOCH_KEY] == pytest.approx(_epoch("2026-04-28T05:00:00Z"))


def test_parse_time_filter_raises_with_param_name():
    with pytest.raises(ValueError, match="time_range_start must be an ISO 8601"):
        parse_time_filter("28/04/2026", "time_range_start")


# ---------------------------------------------------------------------------
# Unit: where-filter construction
# ---------------------------------------------------------------------------


def test_where_filter_uses_numeric_operands_on_epoch_fields():
    where = logs_mod._build_log_where_filter(
        severity="ERROR",
        device_id="",
        event_type="",
        time_range_start=_epoch(WINDOW_START),
        time_range_end=_epoch(WINDOW_END),
    )
    conds = where["$and"]
    assert {"file_type": "log"} in conds
    assert {"severity": "error"} in conds

    start_cond = next(c for c in conds if TS_END_EPOCH_KEY in c)
    end_cond = next(c for c in conds if TS_START_EPOCH_KEY in c)
    # Overlap semantics: chunk_end >= window_start AND chunk_start <= window_end
    assert start_cond == {TS_END_EPOCH_KEY: {"$gte": _epoch(WINDOW_START)}}
    assert end_cond == {TS_START_EPOCH_KEY: {"$lte": _epoch(WINDOW_END)}}
    assert isinstance(start_cond[TS_END_EPOCH_KEY]["$gte"], float)
    assert isinstance(end_cond[TS_START_EPOCH_KEY]["$lte"], float)

    # No ISO-string comparisons anywhere in the filter
    assert not any("timestamp_range_start" in c or "timestamp_range_end" in c for c in conds)


def test_where_filter_without_time_has_no_epoch_conditions():
    where = logs_mod._build_log_where_filter("", "", "", None, None)
    assert where == {"file_type": "log"}


# ---------------------------------------------------------------------------
# Unit: index-time metadata carries epoch fields
# ---------------------------------------------------------------------------


def test_log_indexer_metadata_contains_epoch_fields():
    from rag_mcp.log.log_indexer import LogIndexer

    group = MagicMock()
    group.event_type = "error"
    group.severity = "error"
    group.device_id = "d"
    group.error_code = ""
    group.timestamp_start = "2026-04-28T05:10:00Z"
    group.timestamp_end = "2026-04-28T05:20:00Z"
    group.line_start = 1
    group.line_end = 2

    meta = LogIndexer._build_chunk_metadata(
        MagicMock(), group=group, file_path="f.log", project_name="p",
        source_desc="", chunk_index=0, total_chunks=1,
    )
    assert meta["timestamp_range_start"] == "2026-04-28T05:10:00Z"
    assert meta[TS_START_EPOCH_KEY] == pytest.approx(_epoch("2026-04-28T05:10:00Z"))
    assert meta[TS_END_EPOCH_KEY] == pytest.approx(_epoch("2026-04-28T05:20:00Z"))


# ---------------------------------------------------------------------------
# Integration: real ChromaDB, filter-only path
# ---------------------------------------------------------------------------


def test_time_range_returns_overlapping_events(ctx, store):
    col = store.get_or_create_collection("logs")
    _add_three_events(col)

    out = logs_mod._search_logs_sync(
        severity="ERROR", project="logs",
        time_range_start=WINDOW_START, time_range_end=WINDOW_END,
    )

    assert "Found 2 log result(s)" in out
    assert "event inside window" in out
    assert "event straddling window end" in out
    assert "event before window" not in out


def test_open_ended_time_range(ctx, store):
    col = store.get_or_create_collection("logs")
    _add_three_events(col)

    only_start = logs_mod._search_logs_sync(project="logs", time_range_start=WINDOW_START)
    assert "Found 2 log result(s)" in only_start
    assert "event before window" not in only_start

    only_end = logs_mod._search_logs_sync(project="logs", time_range_end="2026-04-28T04:00:00Z")
    assert "Found 1 log result(s)" in only_end
    assert "event before window" in only_end


def test_time_range_combined_with_other_filters(ctx, store):
    col = store.get_or_create_collection("logs")
    _add_three_events(col)
    col.add(
        ids=["inside-info"],
        embeddings=[[0.1, 0.2, 0.3]],
        documents=["info event inside window"],
        metadatas=[_log_meta("2026-04-28T05:30:00Z", "2026-04-28T05:31:00Z", severity="info")],
    )

    out = logs_mod._search_logs_sync(
        severity="INFO", project="logs",
        time_range_start=WINDOW_START, time_range_end=WINDOW_END,
    )
    assert "Found 1 log result(s)" in out
    assert "info event inside window" in out


def test_time_range_across_all_collections(ctx, store):
    """No project given: every prefixed collection is searched."""
    _add_three_events(store.get_or_create_collection("logs_a"))
    col_b = store.get_or_create_collection("logs_b")
    col_b.add(
        ids=["b-inside"], embeddings=[[0.1, 0.2, 0.3]], documents=["b event inside"],
        metadatas=[_log_meta("2026-04-28T05:40:00Z", "2026-04-28T05:41:00Z")],
    )

    out = logs_mod._search_logs_sync(time_range_start=WINDOW_START, time_range_end=WINDOW_END)
    assert "Found 3 log result(s)" in out
    assert "b event inside" in out


def test_malformed_time_returns_explicit_error(ctx, store):
    store.get_or_create_collection("logs")
    out = logs_mod._search_logs_sync(project="logs", time_range_start="28/04/2026")
    assert out.startswith("Error:")
    assert "time_range_start" in out
    assert "ISO 8601" in out


def test_inverted_time_range_returns_explicit_error(ctx, store):
    store.get_or_create_collection("logs")
    out = logs_mod._search_logs_sync(
        project="logs", time_range_start=WINDOW_END, time_range_end=WINDOW_START,
    )
    assert out == "Error: time_range_start must not be after time_range_end."


def test_query_errors_are_reported_not_swallowed(ctx, store, capsys):
    """If every collection fails, the user sees an error, not 'Found 0'."""
    store.get_or_create_collection("logs")
    col = store.get_collection("logs")
    col.get = MagicMock(side_effect=RuntimeError("db exploded"))
    store.get_collection = MagicMock(return_value=col)

    out = logs_mod._search_logs_sync(project="logs", severity="ERROR")
    assert out.startswith("Error: search_logs failed for all collections.")
    assert "RuntimeError: db exploded" in out
    assert "Found 0" not in out


# ---------------------------------------------------------------------------
# Lazy backfill of legacy (string-only) log chunks
# ---------------------------------------------------------------------------


def test_regression_legacy_string_chunks_no_longer_silent(ctx, store):
    """Exact pre-fix scenario: chunks indexed with only ISO strings.

    Before the fix ChromaDB raised ValueError on the string $gte, the error
    was swallowed and the tool returned 'Found 0'. Now the legacy chunks are
    backfilled on first use and the correct results come back.
    """
    col = store.get_or_create_collection("logs")
    _add_three_events(col, with_epoch=False)
    # Sanity: legacy state really has no epoch fields
    assert all(TS_START_EPOCH_KEY not in m for m in col.get(include=["metadatas"])["metadatas"])

    out = logs_mod._search_logs_sync(
        severity="ERROR", project="logs",
        time_range_start=WINDOW_START, time_range_end=WINDOW_END,
    )

    assert "Found 2 log result(s)" in out
    assert "event inside window" in out
    assert "event straddling window end" in out
    assert "event before window" not in out


def test_backfill_adds_epoch_fields_and_is_idempotent(store):
    col = store.get_or_create_collection("logs")
    _add_three_events(col, with_epoch=False)
    # A non-log chunk and a log chunk with no timestamp must be left alone
    col.add(
        ids=["doc", "no-ts"],
        embeddings=[[0.1, 0.2, 0.3]] * 2,
        documents=["a document", "log without timestamp"],
        metadatas=[
            {"file_path": "README.md", "file_type": "documentation"},
            _log_meta("", "", with_epoch=False),
        ],
    )

    updated = store.backfill_log_epochs(col)
    assert updated == 3

    rows = col.get(include=["metadatas"])
    by_id = dict(zip(rows["ids"], rows["metadatas"]))
    assert by_id["inside"][TS_START_EPOCH_KEY] == pytest.approx(_epoch("2026-04-28T05:10:00Z"))
    assert by_id["inside"][TS_END_EPOCH_KEY] == pytest.approx(_epoch("2026-04-28T05:20:00Z"))
    # Existing metadata preserved (update merges, not replaces)
    assert by_id["inside"]["timestamp_range_start"] == "2026-04-28T05:10:00Z"
    assert by_id["inside"]["severity"] == "error"
    assert by_id["inside"]["line_start"] == 1
    # Untouched records
    assert TS_START_EPOCH_KEY not in by_id["doc"]
    assert TS_START_EPOCH_KEY not in by_id["no-ts"]

    # Second run: nothing left to do
    assert store.backfill_log_epochs(col) == 0


def test_backfill_only_touches_records_missing_epochs(store):
    col = store.get_or_create_collection("logs")
    col.add(
        ids=["new", "old"],
        embeddings=[[0.1, 0.2, 0.3]] * 2,
        documents=["new-format chunk", "old-format chunk"],
        metadatas=[
            _log_meta("2026-04-28T05:10:00Z", "2026-04-28T05:20:00Z", with_epoch=True),
            _log_meta("2026-04-28T05:30:00Z", "2026-04-28T05:40:00Z", with_epoch=False),
        ],
    )
    assert store.backfill_log_epochs(col) == 1


def test_backfill_runs_once_per_collection_per_process(ctx, store):
    col = store.get_or_create_collection("logs")
    _add_three_events(col, with_epoch=False)
    spy = MagicMock(wraps=store.backfill_log_epochs)
    store.backfill_log_epochs = spy

    for _ in range(3):
        logs_mod._search_logs_sync(project="logs", time_range_start=WINDOW_START)
    assert spy.call_count == 1

    # Searches without a time filter never trigger the backfill
    spy.reset_mock()
    logs_mod._search_logs_sync(project="logs", severity="ERROR")
    spy.assert_not_called()


def test_mixed_legacy_and_new_chunks_all_searchable(ctx, store):
    col = store.get_or_create_collection("logs")
    col.add(
        ids=["new", "old"],
        embeddings=[[0.1, 0.2, 0.3]] * 2,
        documents=["new-format chunk", "old-format chunk"],
        metadatas=[
            _log_meta("2026-04-28T05:10:00Z", "2026-04-28T05:20:00Z", with_epoch=True),
            _log_meta("2026-04-28T05:30:00Z", "2026-04-28T05:40:00Z", with_epoch=False),
        ],
    )
    out = logs_mod._search_logs_sync(
        project="logs", time_range_start=WINDOW_START, time_range_end=WINDOW_END,
    )
    assert "Found 2 log result(s)" in out
    assert "new-format chunk" in out
    assert "old-format chunk" in out
