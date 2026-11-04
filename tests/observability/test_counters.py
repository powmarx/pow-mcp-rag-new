"""Tests for in-process counters and JSONL replay (rag_mcp.observability).

  * every emitted event is folded into the live counters, thread-safely
  * ``get_metrics_snapshot()`` has the documented shape and is JSON-serialisable
  * ``aggregate_events_from_log()`` replays a file into the SAME shape and the
    SAME numbers, skipping malformed/unknown lines and tolerating a missing file
  * ``format_metrics_text()`` renders both variants
"""

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import observability
from rag_mcp.embedding_generator import EmbeddingGenerator
from rag_mcp.tools.helpers import log_tool_call

SECTIONS = {"tool_calls", "embed_failures", "log_epoch_backfills"}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    observability.reset_counters()
    yield
    observability.configure(None)
    observability.reset_counters()


def _fail_embed(kind="documents", exc=None, n=2):
    gen = EmbeddingGenerator("m")
    gen.model = MagicMock()
    gen.model.encode.side_effect = exc or RuntimeError("boom")
    with pytest.raises(type(exc) if exc else RuntimeError):
        gen.encode_query("q") if kind == "query" else gen.encode(["x"] * n)


def _backfill(collection="rag_logs", checked=5, missing=3, updated=3):
    observability.log_log_epoch_backfill_event(collection, checked, missing, updated, time.time())


# ---------------------------------------------------------------------------
# Live counters
# ---------------------------------------------------------------------------


def test_fresh_snapshot_is_zeroed_with_documented_shape():
    snap = observability.get_metrics_snapshot()
    assert snap["source"] == "live"
    assert set(snap["process"]) == {"pid", "started_at", "uptime_seconds"}
    assert SECTIONS <= snap.keys()
    assert snap["tool_calls"] == {"total": 0, "errors": 0, "by_tool": {}}
    assert snap["embed_failures"] == {"total": 0, "by_kind": {}, "by_error_type": {}}
    assert snap["log_epoch_backfills"] == {"events": 0, "records_updated_total": 0, "by_collection": {}}
    json.dumps(snap)  # must be serialisable


def test_tool_calls_are_counted_per_tool_with_durations(capsys):
    log_tool_call("search_docs", {"query": "a"}, time.time() - 0.100, 5)
    log_tool_call("search_docs", {"query": "b"}, time.time() - 0.300, 0)
    log_tool_call("list_projects", {}, time.time(), 2)

    tc = observability.get_metrics_snapshot()["tool_calls"]
    assert tc["total"] == 3
    assert set(tc["by_tool"]) == {"search_docs", "list_projects"}
    sd = tc["by_tool"]["search_docs"]
    assert sd["count"] == 2
    assert sd["zero_result_calls"] == 1
    assert 300 <= sd["max_duration_ms"] < 5000
    assert 200 <= sd["avg_duration_ms"] <= sd["max_duration_ms"]
    assert sd["total_duration_ms"] == pytest.approx(sd["avg_duration_ms"] * 2, abs=0.2)
    assert tc["by_tool"]["list_projects"]["zero_result_calls"] == 0
    assert tc["errors"] == 0
    assert sd["error_calls"] == 0


def test_error_calls_are_counted_separately_from_zero_results(capsys):
    log_tool_call("search_docs", {"query": "a"}, time.time() - 0.100, 3)
    log_tool_call("search_docs", {"query": "b"}, time.time() - 0.100, 0)
    log_tool_call("search_docs", {"query": ""}, time.time() - 0.400, 0, outcome="error", error="Error: empty")
    log_tool_call("search_docs", {"query": "c", "project": "nope"}, time.time(), 0, outcome="error", error="Error: nf")
    log_tool_call("list_projects", {}, time.time(), 0)

    tc = observability.get_metrics_snapshot()["tool_calls"]
    assert tc["total"] == 5
    assert tc["errors"] == 2
    sd = tc["by_tool"]["search_docs"]
    assert sd["count"] == 4, "count covers every call, ok or error"
    assert sd["error_calls"] == 2
    assert sd["zero_result_calls"] == 1, "an error is not an empty answer (D4)"
    assert sd["error_calls"] + sd["zero_result_calls"] <= sd["count"], "disjoint subsets"
    assert 400 <= sd["max_duration_ms"] < 5000, "durations include error calls"
    assert sd["total_duration_ms"] == pytest.approx(sd["avg_duration_ms"] * 4, abs=0.3)
    lp = tc["by_tool"]["list_projects"]
    assert (lp["count"], lp["error_calls"], lp["zero_result_calls"]) == (1, 0, 1)


def test_snapshot_by_tool_has_documented_keys(capsys):
    log_tool_call("a", {}, time.time(), 1)
    t = observability.get_metrics_snapshot()["tool_calls"]["by_tool"]["a"]
    assert set(t) == {
        "count", "total_duration_ms", "avg_duration_ms", "max_duration_ms", "error_calls", "zero_result_calls",
    }


def test_embed_failures_are_counted_by_kind_and_error_type(capsys):
    _fail_embed("documents", RuntimeError("a"))
    _fail_embed("documents", ValueError("b"))
    _fail_embed("query", RuntimeError("c"))

    ef = observability.get_metrics_snapshot()["embed_failures"]
    assert ef["total"] == 3
    assert ef["by_kind"] == {"documents": 2, "query": 1}
    assert ef["by_error_type"] == {"RuntimeError": 2, "ValueError": 1}


def test_backfills_are_counted_per_collection(capsys):
    _backfill("rag_logs", updated=3)
    _backfill("rag_logs", updated=2)
    _backfill("rag_other", updated=10)

    bf = observability.get_metrics_snapshot()["log_epoch_backfills"]
    assert bf["events"] == 3
    assert bf["records_updated_total"] == 15
    assert bf["by_collection"] == {
        "rag_logs": {"events": 2, "records_updated": 5},
        "rag_other": {"events": 1, "records_updated": 10},
    }


def test_reset_counters_zeroes_everything(capsys):
    log_tool_call("a", {}, time.time(), 1)
    _fail_embed()
    _backfill()
    observability.reset_counters()
    snap = observability.get_metrics_snapshot()
    assert (snap["tool_calls"]["total"], snap["embed_failures"]["total"], snap["log_epoch_backfills"]["events"]) == (0, 0, 0)


def test_counters_are_thread_safe(capsys):
    n_threads, n_calls = 20, 100

    def worker(i):
        for j in range(n_calls):
            # j % 4: 0 -> ok/0, 1 -> ok/1, 2 -> error, 3 -> ok/1
            if j % 4 == 2:
                log_tool_call(f"tool{i % 3}", {"j": j}, time.time(), 0, outcome="error", error="Error: x")
            else:
                log_tool_call(f"tool{i % 3}", {"j": j}, time.time(), 0 if j % 4 == 0 else 1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tc = observability.get_metrics_snapshot()["tool_calls"]
    assert tc["total"] == n_threads * n_calls, "no lost increments"
    assert tc["errors"] == n_threads * n_calls // 4
    assert sum(t["error_calls"] for t in tc["by_tool"].values()) == n_threads * n_calls // 4
    assert sum(t["zero_result_calls"] for t in tc["by_tool"].values()) == n_threads * n_calls // 4


def test_counting_does_not_depend_on_file_sink(capsys, tmp_path):
    observability.configure(None)  # stderr only
    log_tool_call("a", {}, time.time(), 1)
    observability.configure(tmp_path / "events.jsonl")
    log_tool_call("a", {}, time.time(), 1)
    assert observability.get_metrics_snapshot()["tool_calls"]["total"] == 2


# ---------------------------------------------------------------------------
# JSONL replay
# ---------------------------------------------------------------------------


def test_replay_matches_live_counters_for_the_same_events(tmp_path, capsys):
    log_file = tmp_path / "events.jsonl"
    observability.configure(log_file)

    log_tool_call("search_docs", {"query": "a"}, time.time() - 0.05, 3)
    log_tool_call("search_docs", {"query": "b"}, time.time(), 0)
    log_tool_call("find_variable", {"variable_name": "X"}, time.time(), 1)
    log_tool_call("find_variable", {"variable_name": ""}, time.time(), 0, outcome="error", error="Error: required")
    _fail_embed("documents", RuntimeError("boom"))
    _fail_embed("query", ValueError("bad"))
    _backfill("rag_logs", 5, 3, 3)

    live = observability.get_metrics_snapshot()
    replayed = observability.aggregate_events_from_log(log_file)

    assert replayed["source"] == "jsonl_replay"
    assert set(replayed) - {"file"} == set(live) - {"process"}
    for section in SECTIONS:
        assert replayed[section] == live[section], section
    f = replayed["file"]
    assert (f["lines_total"], f["lines_parsed"], f["lines_skipped"], f["process_count"]) == (7, 7, 0, 1)
    assert replayed["tool_calls"]["errors"] == 1
    assert replayed["tool_calls"]["by_tool"]["find_variable"]["error_calls"] == 1
    assert f["first_ts"] <= f["last_ts"]
    assert f["exists"] is True and f["path"] == str(log_file)


def test_replay_skips_malformed_blank_and_unknown_lines(tmp_path):
    log_file = tmp_path / "events.jsonl"
    good = {"ts": "2026-04-28T05:00:00.000Z", "level": "INFO", "event": "tool_call", "pid": 1,
            "message": "m", "tool": "search_docs", "params": {}, "duration_ms": 10.0, "result_count": 1}
    lines = [
        json.dumps(good),
        "",
        '{"ts":"2026-04-28T05:00:01.000Z","event":"tool_call","tool":"search_do',  # truncated mid-write
        "not json at all",
        "[1, 2, 3]",  # valid JSON, not an object
        json.dumps({**good, "ts": "2026-04-28T05:00:02.000Z", "pid": 2, "event": "future_event_type"}),
        json.dumps({**good, "ts": "2026-04-28T05:00:03.000Z", "pid": 2, "result_count": 0}),
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    snap = observability.aggregate_events_from_log(log_file)

    assert snap["file"]["lines_total"] == 6  # blank line not counted
    assert snap["file"]["lines_parsed"] == 2
    assert snap["file"]["lines_skipped"] == 4
    assert snap["file"]["process_count"] == 2
    assert snap["file"]["first_ts"] == "2026-04-28T05:00:00.000Z"
    assert snap["file"]["last_ts"] == "2026-04-28T05:00:03.000Z"
    assert snap["tool_calls"]["by_tool"]["search_docs"] == {
        "count": 2, "total_duration_ms": 20.0, "avg_duration_ms": 10.0, "max_duration_ms": 10.0,
        "error_calls": 0, "zero_result_calls": 1,
    }


def test_replay_treats_records_without_outcome_as_ok(tmp_path):
    """Files written before ``outcome`` existed (and unknown values from newer
    writers) must still replay, as successes (D5)."""
    base = {"ts": "2026-04-28T05:00:00.000Z", "level": "INFO", "event": "tool_call", "pid": 1,
            "message": "m", "tool": "search_docs", "params": {}, "duration_ms": 10.0}
    lines = [
        json.dumps({**base, "result_count": 0}),                                   # legacy, no outcome
        json.dumps({**base, "result_count": 2}),                                   # legacy, no outcome
        json.dumps({**base, "result_count": 0, "outcome": "weird"}),               # unknown value
        json.dumps({**base, "result_count": 0, "outcome": "error", "error": "Error: x"}),
        json.dumps({**base, "result_count": 0, "outcome": "ok"}),
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    snap = observability.aggregate_events_from_log(tmp_path / "events.jsonl")

    assert snap["file"]["lines_skipped"] == 0
    t = snap["tool_calls"]["by_tool"]["search_docs"]
    assert t["count"] == 5
    assert t["error_calls"] == 1
    assert t["zero_result_calls"] == 3, "legacy/unknown-outcome zero-result records still count as empty successes"
    assert snap["tool_calls"]["errors"] == 1


def test_replay_of_missing_file_is_zeroed_not_an_error(tmp_path):
    snap = observability.aggregate_events_from_log(tmp_path / "nope.jsonl")
    assert snap["file"]["exists"] is False
    assert snap["file"]["lines_total"] == 0 and snap["file"]["first_ts"] is None
    assert snap["tool_calls"]["total"] == 0
    json.dumps(snap)


def test_replay_does_not_touch_live_counters(tmp_path, capsys):
    log_file = tmp_path / "events.jsonl"
    observability.configure(log_file)
    log_tool_call("a", {}, time.time(), 1)
    observability.aggregate_events_from_log(log_file)
    observability.aggregate_events_from_log(log_file)
    assert observability.get_metrics_snapshot()["tool_calls"]["total"] == 1


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def test_format_text_for_replay_snapshot(tmp_path, capsys):
    log_file = tmp_path / "events.jsonl"
    observability.configure(log_file)
    log_tool_call("search_docs", {"query": "a"}, time.time(), 0)
    _fail_embed("query", RuntimeError("boom"))
    _backfill("rag_logs", 5, 3, 3)

    text = observability.format_metrics_text(observability.aggregate_events_from_log(log_file))

    assert f"Event log : {log_file}" in text
    assert "Events    : 3 parsed, 0 skipped" in text
    assert "Tool calls: 1\n" in text, "no '(n errors)' suffix when there are none"
    assert "errors" in text.splitlines()[text.splitlines().index("Tool calls: 1") + 1]  # column header
    assert "search_docs" in text
    assert "Embed failures: 1" in text and "query=1" in text and "RuntimeError=1" in text
    assert "Log-epoch backfills: 1 event(s), 3 record(s) updated" in text
    assert "rag_logs" in text


def test_format_text_shows_errors_column_and_suffix(capsys):
    log_tool_call("search_docs", {"query": "a"}, time.time(), 0)
    log_tool_call("search_docs", {"query": ""}, time.time(), 0, outcome="error", error="Error: empty")
    log_tool_call("list_files", {"project": "x"}, time.time(), 0, outcome="error", error="Error: nf")

    text = observability.format_metrics_text(observability.get_metrics_snapshot())
    lines = text.splitlines()

    assert "Tool calls: 3 (2 errors)" in lines
    header = lines[lines.index("Tool calls: 3 (2 errors)") + 1]
    assert header.split() == ["tool", "calls", "errors", "avg", "ms", "max", "ms", "zero-result"]
    rows = {ln.split()[0]: ln.split() for ln in lines if ln.startswith("  search_docs") or ln.startswith("  list_files")}
    # tool, calls, errors, avg, max, zero-result
    assert rows["search_docs"][1:3] == ["2", "1"] and rows["search_docs"][-1] == "1"
    assert rows["list_files"][1:3] == ["1", "1"] and rows["list_files"][-1] == "0"


def test_format_text_singular_error_suffix(capsys):
    log_tool_call("a", {}, time.time(), 0, outcome="error", error="Error: x")
    assert "Tool calls: 1 (1 error)" in observability.format_metrics_text(observability.get_metrics_snapshot())


def test_format_text_for_missing_file(tmp_path):
    text = observability.format_metrics_text(observability.aggregate_events_from_log(tmp_path / "nope.jsonl"))
    assert "file not found" in text
    assert "Tool calls: 0" in text and "Embed failures: 0" in text and "Log-epoch backfills: 0" in text


def test_format_text_for_live_snapshot(capsys):
    log_tool_call("list_projects", {}, time.time(), 2)
    text = observability.format_metrics_text(observability.get_metrics_snapshot())
    assert text.startswith("Process   : pid ")
    assert "(live counters)" in text and "list_projects" in text
