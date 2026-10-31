"""Tests for structured ``log_epoch_backfill`` JSON logging.

``ChromaStore.backfill_log_epochs`` lazily adds numeric ``ts_*_epoch`` fields
to legacy log chunks. It must:
  * emit exactly one ``log_epoch_backfill`` JSON line on stderr and append the
    identical line to the JSONL file — but ONLY when at least one record was
    updated (a no-op check emits nothing),
  * keep its return value / data effects exactly as before.

Runs against a real in-memory ChromaDB (EphemeralClient); no embedding model.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import chromadb
import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import observability
from rag_mcp.chroma_store import ChromaStore
from rag_mcp.config_loader import StorageConfig
from rag_mcp.log.timestamps import TS_START_EPOCH_KEY, epoch_fields
from rag_mcp.tools import logs as logs_mod

REQUIRED_KEYS = {
    "ts", "level", "event", "pid", "message",
    "collection", "records_checked", "records_missing", "records_updated", "duration_ms",
}
PREFIX = "rag"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_observability_state(monkeypatch):
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    yield
    observability.configure(None)


@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "events.jsonl"
    observability.configure(path)
    return path


@pytest.fixture
def store():
    s = ChromaStore(StorageConfig(path="unused", collection_prefix=PREFIX))
    client = chromadb.EphemeralClient()
    for c in client.list_collections():  # process-wide singleton: start clean
        client.delete_collection(c.name)
    s.client = client
    return s


def _log_meta(start_iso: str, end_iso: str, with_epoch: bool) -> dict:
    meta = {
        "file_path": "device.log", "file_type": "log", "project": "logs",
        "severity": "error", "event_type": "error", "device_id": "dev1",
        "timestamp_range_start": start_iso, "timestamp_range_end": end_iso,
        "line_start": 1, "line_end": 2, "record_type": "log_event",
    }
    if with_epoch:
        meta.update(epoch_fields(start_iso, end_iso))
    return meta


def _add_legacy_chunks(col, n: int, with_epoch: bool = False, prefix: str = "legacy"):
    col.add(
        ids=[f"{prefix}{i}" for i in range(n)],
        embeddings=[[0.1, 0.2, 0.3]] * n,
        documents=[f"event {i}" for i in range(n)],
        metadatas=[_log_meta(f"2026-04-28T05:{i:02d}:00Z", f"2026-04-28T05:{i:02d}:30Z", with_epoch) for i in range(n)],
    )


def _stderr_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def _backfill_events(lines: list[str]) -> list[dict]:
    recs = [json.loads(ln) for ln in lines if ln.startswith("{")]
    return [r for r in recs if r["event"] == "log_epoch_backfill"]


def _file_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Event contract
# ---------------------------------------------------------------------------


def test_backfill_with_updates_emits_one_event_on_stderr_and_file(store, capsys, log_file):
    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 3)

    updated = store.backfill_log_epochs(col)

    assert updated == 3  # return value unchanged
    events = _backfill_events(_stderr_lines(capsys))
    assert len(events) == 1
    rec = events[0]
    assert REQUIRED_KEYS <= rec.keys()
    assert rec["level"] == "INFO"
    assert rec["collection"] == "rag_logs"
    assert rec["records_checked"] == 3
    assert rec["records_missing"] == 3
    assert rec["records_updated"] == 3
    assert isinstance(rec["duration_ms"], float) and rec["duration_ms"] >= 0
    assert rec["message"].startswith("backfilled epoch timestamps on 3/3 legacy log chunks in rag_logs (3 checked) in ")
    assert _file_records(log_file) == events


def test_noop_backfill_emits_nothing(store, capsys, log_file):
    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 2, with_epoch=True)  # already current

    assert store.backfill_log_epochs(col) == 0
    assert _backfill_events(_stderr_lines(capsys)) == []
    assert not log_file.exists()


def test_empty_collection_emits_nothing(store, capsys, log_file):
    col = store.get_or_create_collection("logs")
    assert store.backfill_log_epochs(col) == 0
    assert _stderr_lines(capsys) == []
    assert not log_file.exists()


def test_second_run_is_silent(store, capsys, log_file):
    """First run updates (event); idempotent second run has nothing to do (no event)."""
    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 3)

    assert store.backfill_log_epochs(col) == 3
    assert store.backfill_log_epochs(col) == 0

    assert len(_backfill_events(_stderr_lines(capsys))) == 1
    assert [r["event"] for r in _file_records(log_file)] == ["log_epoch_backfill"]


def test_unparseable_timestamps_counted_as_missing_but_not_updated(store, capsys):
    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 2)  # 2 fixable
    col.add(  # 1 legacy chunk with garbage timestamps: stays missing forever
        ids=["bad"], embeddings=[[0.1, 0.2, 0.3]], documents=["no time"],
        metadatas=[_log_meta("yesterday", "", with_epoch=False)],
    )
    col.add(  # a non-log chunk must not be counted at all
        ids=["doc"], embeddings=[[0.1, 0.2, 0.3]], documents=["a document"],
        metadatas=[{"file_path": "README.md", "file_type": "documentation"}],
    )

    assert store.backfill_log_epochs(col) == 2

    rec = _backfill_events(_stderr_lines(capsys))[0]
    assert (rec["records_checked"], rec["records_missing"], rec["records_updated"]) == (3, 3, 2)
    rows = col.get(include=["metadatas"])
    by_id = dict(zip(rows["ids"], rows["metadatas"]))
    assert TS_START_EPOCH_KEY not in by_id["bad"] and TS_START_EPOCH_KEY not in by_id["doc"]


def test_all_missing_unparseable_means_no_update_and_no_event(store, capsys, log_file):
    col = store.get_or_create_collection("logs")
    col.add(
        ids=["bad"], embeddings=[[0.1, 0.2, 0.3]], documents=["no time"],
        metadatas=[_log_meta("", "", with_epoch=False)],
    )
    assert store.backfill_log_epochs(col) == 0
    assert _backfill_events(_stderr_lines(capsys)) == []
    assert not log_file.exists()


def test_old_free_text_line_is_gone(store, capsys):
    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 1)
    store.backfill_log_epochs(col)
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and lines[0].startswith("{"), "exactly one line, and it is JSON"
    assert not any(ln.startswith("[search_logs]") for ln in lines)


# ---------------------------------------------------------------------------
# Through the real caller: search_logs lazy backfill
# ---------------------------------------------------------------------------


def test_search_logs_triggers_backfill_once_and_logs_both_events(store, capsys, log_file, monkeypatch):
    ctx = MagicMock()
    ctx.store = store
    ctx.config.storage.collection_prefix = PREFIX
    monkeypatch.setattr(logs_mod, "_ctx", ctx)
    monkeypatch.setattr(logs_mod, "_epoch_backfilled_collections", set())

    col = store.get_or_create_collection("logs")
    _add_legacy_chunks(col, 3)

    for _ in range(2):  # second search: backfill already done for this collection
        out = logs_mod._search_logs_sync(
            project="logs", time_range_start="2026-04-28T05:00:30Z", time_range_end="2026-04-28T05:01:15Z",
        )
        assert "Found 2 log result(s)" in out  # legacy chunks now range-filterable

    recs = _file_records(log_file)
    assert [r["event"] for r in recs] == [
        "log_epoch_backfill", "tool_call",  # first search: backfill, then the tool line
        "tool_call",                        # second search: no backfill
    ]
    assert recs[0]["records_updated"] == 3
    assert recs[1]["tool"] == "search_logs"
    assert _backfill_events(_stderr_lines(capsys)) == [recs[0]]
