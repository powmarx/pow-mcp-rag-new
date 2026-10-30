"""Tests for structured ``tool_call`` JSON logging (rag_mcp.observability).

Covers the contract of ``rag_mcp.tools.helpers.log_tool_call``:
  * exactly one JSON line on stderr per call, with the documented envelope
  * the same line appended to the JSONL file when a path is configured
  * path precedence: RAG_OBSERVABILITY_LOG_PATH env > configure() > disabled
  * a tool call never fails because of the log (bad param types, unwritable file)
  * concurrent writers from several threads never interleave lines

No ChromaDB or embedding model is needed: ``log_tool_call`` is exercised
directly and through one real tool (``find_variable``) with a mocked context.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import observability
from rag_mcp.tools.helpers import log_tool_call

REQUIRED_KEYS = {"ts", "level", "event", "pid", "message", "tool", "params", "duration_ms", "result_count"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_observability_state(monkeypatch):
    """Every test starts with no file sink and no env override."""
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    yield
    observability.configure(None)


def _stderr_json_lines(capsys) -> list[dict]:
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON line on stderr: {ln!r}")
    return parsed


def _file_json_lines(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "every record must be newline-terminated"
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# stderr sink
# ---------------------------------------------------------------------------


def test_one_json_line_on_stderr_with_envelope(capsys):
    log_tool_call("search_docs", {"query": "dispense", "project": "", "top_k": 5}, time.time(), 3)

    records = _stderr_json_lines(capsys)
    assert len(records) == 1, "exactly one line per tool call"
    rec = records[0]
    assert REQUIRED_KEYS <= rec.keys()
    assert rec["event"] == "tool_call"
    assert rec["level"] == "INFO"
    assert rec["pid"] == os.getpid()
    assert rec["tool"] == "search_docs"
    assert rec["params"] == {"query": "dispense", "project": "", "top_k": 5}
    assert rec["result_count"] == 3
    assert isinstance(rec["duration_ms"], float)


def test_timestamp_is_iso8601_utc_with_milliseconds(capsys):
    log_tool_call("list_projects", {}, time.time(), 0)
    ts = _stderr_json_lines(capsys)[0]["ts"]
    # 2026-04-28T05:10:00.123Z
    assert len(ts) == 24 and ts.endswith("Z") and ts[10] == "T" and ts[19] == "."
    from datetime import datetime
    datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")  # raises if malformed


def test_message_keeps_human_readable_summary(capsys):
    log_tool_call("search_docs", {"query": "foo", "top_k": 5}, time.time(), 2)
    msg = _stderr_json_lines(capsys)[0]["message"]
    assert msg.startswith("search_docs(query='foo', top_k=5) -> 2 results in ")
    assert msg.endswith("ms")


def test_duration_is_measured_from_start_time(capsys):
    log_tool_call("list_files", {"project": "p"}, time.time() - 0.050, 1)
    rec = _stderr_json_lines(capsys)[0]
    assert rec["duration_ms"] >= 50.0
    assert rec["duration_ms"] < 5000.0


def test_query_is_truncated_to_100_chars_other_params_untouched(capsys):
    long_query = "x" * 250
    log_tool_call("search_docs", {"query": long_query, "project": "y" * 250}, time.time(), 0)
    rec = _stderr_json_lines(capsys)[0]
    assert rec["params"]["query"] == "x" * 100 + "..."
    assert rec["params"]["project"] == "y" * 250
    assert long_query not in rec["message"]


def test_non_json_serializable_param_does_not_break_the_call(capsys):
    folder = Path("some") / "dir"
    log_tool_call("add_folder", {"folder_path": folder, "pattern": "**/*"}, time.time(), 4)
    rec = _stderr_json_lines(capsys)[0]
    assert rec["params"]["folder_path"] == str(folder)
    assert rec["params"]["pattern"] == "**/*"


def test_unicode_is_written_verbatim(capsys):
    log_tool_call("search_docs", {"query": "café → résumé"}, time.time(), 0)
    err = capsys.readouterr().err
    assert "café → résumé" in err  # ensure_ascii=False, no \\u escapes
    assert json.loads(err)["params"]["query"] == "café → résumé"


# ---------------------------------------------------------------------------
# JSONL file sink
# ---------------------------------------------------------------------------


def test_no_file_written_when_unconfigured(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_tool_call("list_projects", {}, time.time(), 0)
    assert observability.resolve_log_path() is None
    assert list(tmp_path.iterdir()) == []
    assert len(_stderr_json_lines(capsys)) == 1  # stderr still gets the line


def test_configured_path_appends_identical_line_to_file(tmp_path, capsys):
    log_file = tmp_path / "obs" / "events.jsonl"  # parent dir does not exist yet
    observability.configure(log_file)

    log_tool_call("search_docs", {"query": "a"}, time.time(), 1)
    log_tool_call("find_variable", {"variable_name": "B", "project": ""}, time.time(), 2)

    stderr_recs = _stderr_json_lines(capsys)
    file_recs = _file_json_lines(log_file)
    assert file_recs == stderr_recs
    assert [r["tool"] for r in file_recs] == ["search_docs", "find_variable"]


def test_file_is_appended_not_truncated_across_configure_calls(tmp_path):
    log_file = tmp_path / "events.jsonl"
    observability.configure(log_file)
    log_tool_call("a", {}, time.time(), 0)
    observability.configure(log_file)  # e.g. a second startup in the same process
    log_tool_call("b", {}, time.time(), 0)
    assert [r["tool"] for r in _file_json_lines(log_file)] == ["a", "b"]


def test_env_var_overrides_configured_path(tmp_path, monkeypatch):
    configured = tmp_path / "configured.jsonl"
    override = tmp_path / "override.jsonl"
    observability.configure(configured)
    monkeypatch.setenv(observability.ENV_LOG_PATH, str(override))

    log_tool_call("list_projects", {}, time.time(), 0)

    assert observability.resolve_log_path() == override
    assert override.exists() and not configured.exists()


def test_env_var_alone_enables_file_sink(tmp_path, monkeypatch):
    target = tmp_path / "events.jsonl"
    monkeypatch.setenv(observability.ENV_LOG_PATH, str(target))
    log_tool_call("list_projects", {}, time.time(), 0)
    assert len(_file_json_lines(target)) == 1


def test_blank_env_var_is_ignored(tmp_path, monkeypatch):
    observability.configure(tmp_path / "events.jsonl")
    monkeypatch.setenv(observability.ENV_LOG_PATH, "   ")
    assert observability.resolve_log_path() == tmp_path / "events.jsonl"


def test_default_log_path_lives_next_to_chroma_data():
    assert observability.default_log_path("/app/data") == Path("/app/data/observability/events.jsonl")


def test_unwritable_file_sink_warns_once_and_never_raises(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # a file where a directory is needed -> OSError on mkdir/open
    observability.configure(blocker / "events.jsonl")

    log_tool_call("a", {}, time.time(), 0)
    log_tool_call("b", {}, time.time(), 0)

    err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    warnings = [ln for ln in err_lines if ln.startswith("[observability] WARNING")]
    json_lines = [json.loads(ln) for ln in err_lines if ln.startswith("{")]
    assert len(warnings) == 1, "warn once, not on every call"
    assert [r["tool"] for r in json_lines] == ["a", "b"], "stderr sink unaffected"


def test_concurrent_writers_never_interleave_lines(tmp_path):
    log_file = tmp_path / "events.jsonl"
    observability.configure(log_file)
    n_threads, n_calls = 8, 50

    def worker(i: int):
        for j in range(n_calls):
            log_tool_call(f"tool{i}", {"query": "q" * 80, "i": i, "j": j}, time.time(), j)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recs = _file_json_lines(log_file)  # json.loads on every line: fails if any interleaved
    assert len(recs) == n_threads * n_calls
    seen = {(r["params"]["i"], r["params"]["j"]) for r in recs}
    assert len(seen) == n_threads * n_calls, "no lost or duplicated records"


# ---------------------------------------------------------------------------
# Through a real tool (call sites untouched)
# ---------------------------------------------------------------------------


def test_real_tool_call_site_emits_event(tmp_path, capsys, monkeypatch):
    """find_variable is a plain sync tool that calls log_tool_call unchanged."""
    from rag_mcp.tools import search as search_mod

    observability.configure(tmp_path / "events.jsonl")

    col = MagicMock()
    col.get.return_value = {
        "ids": ["c1"],
        "documents": ["int NOTE_MAX = 3; // NOTE_MAX used here"],
        "metadatas": [{"file_path": "inc/notes.h", "file_type": "header"}],
    }
    ctx = MagicMock()
    ctx.store.get_collection.return_value = col
    monkeypatch.setattr(search_mod, "_ctx", ctx)

    out = search_mod.find_variable("NOTE_MAX", project="proj")

    assert "Found 'NOTE_MAX' in 1 file(s)" in out
    recs = _stderr_json_lines(capsys)
    assert len(recs) == 1
    assert recs[0]["tool"] == "find_variable"
    assert recs[0]["params"] == {"variable_name": "NOTE_MAX", "project": "proj"}
    assert recs[0]["result_count"] == 1
    assert _file_json_lines(tmp_path / "events.jsonl") == recs
