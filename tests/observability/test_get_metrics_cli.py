"""Tests for ``rag-mcp get_metrics`` (rag_mcp.cli).

The CLI is a separate process from the server, so it REPLAYS the JSONL file:
  * default: human-readable report
  * --json : raw JSON, same shape as the MCP tool (source = "jsonl_replay")
  * path   : --file PATH > RAG_OBSERVABILITY_LOG_PATH > derived from config
  * a missing file is reported, not an error (exit 0)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC_DIR = _REPO_ROOT / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import cli, observability
from rag_mcp.tools.helpers import log_tool_call


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    observability.reset_counters()
    yield
    observability.configure(None)
    observability.reset_counters()


@pytest.fixture
def events_file(tmp_path, capsys):
    """A JSONL file with 3 tool calls, 1 embed failure, 1 backfill (written by the real emitters)."""
    from unittest.mock import MagicMock
    from rag_mcp.embedding_generator import EmbeddingGenerator

    path = tmp_path / "events.jsonl"
    observability.configure(path)
    log_tool_call("search_docs", {"query": "dispense"}, time.time() - 0.04, 5)
    log_tool_call("search_docs", {"query": "nothing"}, time.time() - 0.01, 0)
    log_tool_call("list_projects", {}, time.time(), 2)
    gen = EmbeddingGenerator("BAAI/bge-m3")
    gen.model = MagicMock()
    gen.model.encode.side_effect = RuntimeError("CUDA out of memory")
    with pytest.raises(RuntimeError):
        gen.encode(["a", "b"])
    observability.log_log_epoch_backfill_event("rag_logs", 5, 3, 3, time.time())
    observability.configure(None)
    capsys.readouterr()  # discard the emitters' stderr
    return path


def _run_cli(monkeypatch, capsys, *args: str) -> str:
    monkeypatch.setattr(sys, "argv", ["rag-mcp", "get_metrics", *args])
    cli.main()
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def test_human_readable_report_by_default(events_file, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, "--file", str(events_file))

    assert f"Event log : {events_file}" in out
    assert "Events    : 5 parsed, 0 skipped (malformed/unknown), 1 process(es)" in out
    assert "Tool calls: 3" in out
    assert "search_docs" in out and "list_projects" in out
    assert "Embed failures: 1" in out
    assert "documents=1" in out and "RuntimeError=1" in out
    assert "Log-epoch backfills: 1 event(s), 3 record(s) updated" in out
    assert "rag_logs" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # it's a report, not JSON


def test_json_flag_prints_raw_snapshot(events_file, monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, "--json", "--file", str(events_file))

    snap = json.loads(out)
    assert snap["source"] == "jsonl_replay"
    assert snap["file"]["path"] == str(events_file)
    assert snap["file"]["lines_parsed"] == 5
    assert snap["tool_calls"]["total"] == 3
    assert snap["tool_calls"]["by_tool"]["search_docs"]["count"] == 2
    assert snap["tool_calls"]["by_tool"]["search_docs"]["zero_result_calls"] == 1
    assert snap["embed_failures"] == {"total": 1, "by_kind": {"documents": 1}, "by_error_type": {"RuntimeError": 1}}
    assert snap["log_epoch_backfills"]["by_collection"] == {"rag_logs": {"events": 1, "records_updated": 3}}


def test_json_output_has_same_sections_as_mcp_tool(events_file, monkeypatch, capsys):
    from rag_mcp.tools import metrics as metrics_mod

    cli_snap = json.loads(_run_cli(monkeypatch, capsys, "--json", "--file", str(events_file)))
    tool_snap = json.loads(metrics_mod.get_metrics())
    assert set(cli_snap) - {"file"} == set(tool_snap) - {"process"}
    for section in ("tool_calls", "embed_failures", "log_epoch_backfills"):
        assert cli_snap[section].keys() == tool_snap[section].keys()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_env_var_selects_the_file(events_file, monkeypatch, capsys):
    monkeypatch.setenv(observability.ENV_LOG_PATH, str(events_file))
    snap = json.loads(_run_cli(monkeypatch, capsys, "--json"))
    assert snap["file"]["path"] == str(events_file) and snap["tool_calls"]["total"] == 3


def test_file_flag_beats_env_var(events_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(observability.ENV_LOG_PATH, str(tmp_path / "ignored.jsonl"))
    snap = json.loads(_run_cli(monkeypatch, capsys, "--json", "--file", str(events_file)))
    assert snap["file"]["path"] == str(events_file)


def test_default_path_derived_from_config_storage_path(tmp_path, monkeypatch, capsys):
    """No --file, no env var: <storage.path>/observability/events.jsonl, like the server."""
    storage = tmp_path / "chroma-data"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"embedding:\n  model: m\nstorage:\n  path: '{storage}'\n  collection_prefix: rag\nprojects: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_CONFIG_PATH", str(cfg))
    expected = observability.default_log_path(storage)
    expected.parent.mkdir(parents=True)
    observability.configure(expected)
    log_tool_call("search_docs", {"query": "x"}, time.time(), 1)
    observability.configure(None)
    capsys.readouterr()

    snap = json.loads(_run_cli(monkeypatch, capsys, "--json"))
    assert snap["file"]["path"] == str(expected)
    assert snap["tool_calls"]["total"] == 1


def test_relative_storage_path_pip_mode_uses_data_dir(tmp_path, monkeypatch, capsys):
    """No server.py on disk (pip/uvx): mirrors _server._run_inline -> resolve_data_path()."""
    from rag_mcp import _server

    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage:\n  path: './data'\nprojects: []\n", encoding="utf-8")
    monkeypatch.setenv("RAG_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("RAG_DATA_PATH", str(tmp_path / "data-dir"))
    monkeypatch.setattr(_server, "find_server_script", lambda: None)

    snap = json.loads(_run_cli(monkeypatch, capsys, "--json"))
    assert snap["file"]["path"] == str(observability.default_log_path(tmp_path / "data-dir"))


def test_relative_storage_path_repo_mode_uses_server_py_dir(tmp_path, monkeypatch, capsys):
    """server.py on disk (repo checkout, Docker): mirrors server.py -> SCRIPT_DIR / storage.path."""
    from rag_mcp import _server

    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage:\n  path: './data'\nprojects: []\n", encoding="utf-8")
    monkeypatch.setenv("RAG_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("RAG_DATA_PATH", str(tmp_path / "data-dir"))  # must be ignored in this mode
    fake_server_py = tmp_path / "app" / "server.py"
    monkeypatch.setattr(_server, "find_server_script", lambda: fake_server_py)

    snap = json.loads(_run_cli(monkeypatch, capsys, "--json"))
    assert snap["file"]["path"] == str(observability.default_log_path(tmp_path / "app" / "data"))


def test_real_repo_checkout_cli_and_server_py_agree(tmp_path, monkeypatch, capsys):
    """End to end in this checkout: the path the CLI resolves == the one server.py configures."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("storage:\n  path: './data'\nprojects: []\n", encoding="utf-8")
    monkeypatch.setenv("RAG_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("RAG_DATA_PATH", raising=False)

    snap = json.loads(_run_cli(monkeypatch, capsys, "--json"))
    server_py = _REPO_ROOT / "server.py"
    assert server_py.exists()
    assert snap["file"]["path"] == str(observability.default_log_path(server_py.parent / "data"))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_missing_file_is_reported_not_fatal(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope.jsonl"
    out = _run_cli(monkeypatch, capsys, "--file", str(missing))
    assert "file not found" in out and "Tool calls: 0" in out

    snap = json.loads(_run_cli(monkeypatch, capsys, "--json", "--file", str(missing)))
    assert snap["file"]["exists"] is False and snap["tool_calls"]["total"] == 0


def test_truncated_last_line_is_skipped(events_file, monkeypatch, capsys):
    with open(events_file, "a", encoding="utf-8") as fh:
        fh.write('{"ts":"2026-04-28T05:00:00.000Z","event":"tool_call","tool":"sea')  # crash mid-write
    snap = json.loads(_run_cli(monkeypatch, capsys, "--json", "--file", str(events_file)))
    assert snap["file"]["lines_skipped"] == 1
    assert snap["tool_calls"]["total"] == 3


def test_file_flag_without_value_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag-mcp", "get_metrics", "--file"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert "Usage: rag-mcp get_metrics" in capsys.readouterr().err


def test_usage_text_lists_get_metrics(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag-mcp", "bogus"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "get_metrics" in capsys.readouterr().err


def test_subprocess_smoke(events_file):
    """Real process: python -m rag_mcp.cli get_metrics --json -> exit 0, valid JSON on stdout."""
    env = {**os.environ, observability.ENV_LOG_PATH: str(events_file), "PYTHONPATH": str(_SRC_DIR)}
    proc = subprocess.run(
        [sys.executable, "-m", "rag_mcp.cli", "get_metrics", "--json"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    snap = json.loads(proc.stdout)
    assert snap["source"] == "jsonl_replay" and snap["tool_calls"]["total"] == 3
