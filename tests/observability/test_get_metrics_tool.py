"""Tests for the ``get_metrics`` MCP tool (rag_mcp.tools.metrics).

The tool returns the LIVE counters of this process as JSON. It needs no
store or model, so it is exercised directly and through ``register_all_tools``
with a fake FastMCP object (no server boot, no ChromaDB).
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import observability
from rag_mcp.tools import metrics as metrics_mod
from rag_mcp.tools.helpers import log_tool_call


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    observability.reset_counters()
    yield
    observability.configure(None)
    observability.reset_counters()


class _FakeMCP:
    """Captures what ``mcp.tool()(fn)`` registers, like FastMCP would."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def test_get_metrics_returns_live_snapshot_as_json(capsys):
    log_tool_call("search_docs", {"query": "a"}, time.time(), 3)
    log_tool_call("search_docs", {"query": "b"}, time.time(), 0)

    out = metrics_mod.get_metrics()

    snap = json.loads(out)  # valid JSON document
    assert snap["source"] == "live"
    assert snap["tool_calls"]["total"] == 2
    assert snap["tool_calls"]["by_tool"]["search_docs"]["zero_result_calls"] == 1
    assert snap["embed_failures"]["total"] == 0
    assert snap["log_epoch_backfills"]["events"] == 0
    assert snap["process"]["pid"] > 0
    assert out.startswith("{\n  "), "pretty-printed for readability in the client"


def test_get_metrics_reports_error_calls(capsys):
    log_tool_call("get_document", {"file_path": "a.h", "project": "p"}, time.time(), 4)
    log_tool_call("get_document", {"file_path": "", "project": "p"}, time.time(), 0, outcome="error", error="Error: file_path is required")

    snap = json.loads(metrics_mod.get_metrics())
    assert snap["tool_calls"]["total"] == 2
    assert snap["tool_calls"]["errors"] == 1
    gd = snap["tool_calls"]["by_tool"]["get_document"]
    assert gd["count"] == 2
    assert gd["error_calls"] == 1
    assert gd["zero_result_calls"] == 0, "the error must not show up as an empty call"


def test_get_metrics_is_not_itself_counted_or_logged(capsys, tmp_path):
    observability.configure(tmp_path / "events.jsonl")
    before = json.loads(metrics_mod.get_metrics())
    after = json.loads(metrics_mod.get_metrics())
    assert before["tool_calls"]["total"] == after["tool_calls"]["total"] == 0
    assert capsys.readouterr().err == ""
    assert not (tmp_path / "events.jsonl").exists()


def test_get_metrics_reflects_all_three_event_types(capsys):
    from rag_mcp.embedding_generator import EmbeddingGenerator

    log_tool_call("list_projects", {}, time.time(), 1)
    gen = EmbeddingGenerator("m")
    gen.model = MagicMock()
    gen.model.encode.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        gen.encode(["x"])
    observability.log_log_epoch_backfill_event("rag_logs", 3, 3, 3, time.time())

    snap = json.loads(metrics_mod.get_metrics())
    assert snap["tool_calls"]["total"] == 1
    assert snap["embed_failures"]["by_kind"] == {"documents": 1}
    assert snap["log_epoch_backfills"]["records_updated_total"] == 3


def test_get_metrics_is_registered_by_register_all_tools():
    from rag_mcp.tools import register_all_tools

    mcp = _FakeMCP()
    register_all_tools(mcp, MagicMock())

    assert "get_metrics" in mcp.tools
    fn = mcp.tools["get_metrics"]
    assert fn is metrics_mod.get_metrics
    assert fn.__doc__ and "live" in fn.__doc__.lower()  # FastMCP uses the docstring as description
    # still registers everything it did before
    for existing in ("search_docs", "list_projects", "add_project", "search_logs"):
        assert existing in mcp.tools


def test_get_metrics_takes_no_parameters():
    import inspect
    assert list(inspect.signature(metrics_mod.get_metrics).parameters) == []
