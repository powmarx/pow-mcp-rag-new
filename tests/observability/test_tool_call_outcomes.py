"""Every return path of every MCP tool emits exactly one ``tool_call`` event
with the right ``outcome`` (rag_mcp.tools.*).

  * ERROR_PATHS   — one row per early return that used to emit nothing (75)
                    plus the 4 that used to emit as an empty success
  * OK_PATHS      — the judgement calls that stay ``outcome="ok"`` with
                    ``result_count == 0`` (empty answers are not errors)
  * a static guard — every ``return`` in a tool body goes through
                    ``call.ok(...)`` / ``call.error(...)``, so a new early
                    return cannot silently reopen the gap

No ChromaDB, no model: every tool runs against a ``ToolContext`` built from a
real ``AppConfig`` and ``MagicMock`` store / loader / embedding generator, the
same way ``test_tool_call_log.test_real_tool_call_site_emits_event`` does.
Tool return texts are asserted on so the client-visible strings are proven
unchanged by the conversion.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_SRC_DIR = _REPO_ROOT / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp import observability
from rag_mcp.config_loader import (
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    ProjectConfig,
    SourcePattern,
    StorageConfig,
)
from rag_mcp.tools import ToolContext, documents, logs, management, search

_MODULES = {"search": search, "documents": documents, "management": management, "logs": logs}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_observability_state(monkeypatch):
    monkeypatch.delenv(observability.ENV_LOG_PATH, raising=False)
    observability.configure(None)
    observability.reset_counters()
    yield
    observability.configure(None)
    observability.reset_counters()


def _events(capsys, tool: str) -> list[dict]:
    """All ``tool_call`` records on stderr for one tool (nested ``search_docs``
    events from find_function / compare_projects are filtered out here)."""
    out = []
    for ln in capsys.readouterr().err.splitlines():
        if not ln.startswith("{"):
            continue  # e.g. "[search_logs] Error querying collection ..." free-text lines
        rec = json.loads(ln)
        if rec.get("event") == "tool_call" and rec.get("tool") == tool:
            out.append(rec)
    return out


def _collection(ids: list[str] | None = None, **overrides) -> MagicMock:
    """A mocked ChromaDB collection whose get()/query() return ``ids``."""
    ids = ids or []
    col = MagicMock()
    col.name = "rag_proj"
    col.count.return_value = len(ids)
    col.get.return_value = {
        "ids": ids,
        "documents": ["doc " + i for i in ids],
        "metadatas": [{"file_path": f"{i}.h", "file_type": "header", "chunk_index": n} for n, i in enumerate(ids)],
    }
    col.query.return_value = {
        "documents": [["doc " + i for i in ids]] if ids else [[]],
        "metadatas": [[{"file_path": f"{i}.h", "file_type": "header"} for i in ids]] if ids else [[]],
        "distances": [[0.1] * len(ids)] if ids else [[]],
    }
    for k, v in overrides.items():
        setattr(col, k, v)
    return col


def make_ctx(
    tmp_path: Path,
    projects: list[ProjectConfig] | None = None,
    collection: MagicMock | None = None,
    list_collections: list | None = None,
) -> ToolContext:
    """Real AppConfig, mocked everything else.

    ``collection``: what ``store.get_collection(<any name>)`` returns (None = "not found").
    ``list_collections``: what ``store.list_collections()`` returns (default: empty).
    """
    config = AppConfig(
        embedding=EmbeddingConfig(),
        storage=StorageConfig(path=str(tmp_path / "data")),
        chunking=ChunkingConfig(),
        projects=list(projects or []),
    )
    config.reranker.enabled = False
    store = MagicMock()
    store.get_collection.return_value = collection
    store.list_collections.return_value = list(list_collections or [])
    store.client.get_collection.return_value = collection
    embedding_gen = MagicMock()
    embedding_gen.encode_query.return_value = [0.0, 0.0]
    embedding_gen.encode.return_value = [[0.0, 0.0]]
    return ToolContext(
        config=config,
        loader=MagicMock(),
        store=store,
        embedding_gen=embedding_gen,
        ensure_model_loaded=MagicMock(),
        reindex_in_progress=lambda: False,
        indexing_cancelled=lambda: False,
        set_indexing_cancelled=lambda v: None,
        reranker=None,
        ensure_reranker_loaded=MagicMock(),
    )


def _project(tmp_path: Path, name: str = "proj", removed: bool = False, sources=None) -> ProjectConfig:
    base = tmp_path / name
    base.mkdir(exist_ok=True)
    return ProjectConfig(
        name=name, description="d", base_path=str(base), removed=removed, sources=list(sources or [])
    )


def _meta(name: str) -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


# Each row: (module, function, tool name, kwargs, ctx/patch setup, expected substring of the returned text)
# ``setup(tmp_path, monkeypatch) -> ToolContext``. kwargs may be a callable taking tmp_path.
Row = tuple[str, str, str, Any, Callable[[Path, Any], ToolContext], str]


def _dir(tmp_path: Path, name: str) -> str:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _file(tmp_path: Path, name: str, text: str = "int x;\n") -> str:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return str(f)


# ---------------------------------------------------------------------------
# ERROR paths — 75 formerly silent early returns + 4 reclassified
# ---------------------------------------------------------------------------

ERROR_PATHS: list[Row] = [
    # ---- search._search_docs_sync (5) ----
    ("search", "_search_docs_sync", "search_docs", {"query": "   "}, lambda t, m: make_ctx(t), "query must not be empty"),
    ("search", "_search_docs_sync", "search_docs", {"query": "x" * 1001}, lambda t, m: make_ctx(t), "must not exceed 1000"),
    ("search", "_search_docs_sync", "search_docs", {"query": "q"},
     lambda t, m: _with(make_ctx(t), lambda c: setattr(c.embedding_gen.encode_query, "side_effect", RuntimeError("boom"))),
     "Error encoding query: boom"),
    ("search", "_search_docs_sync", "search_docs", {"query": "q", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("search", "_search_docs_sync", "search_docs", {"query": "q"}, lambda t, m: make_ctx(t), "No indexed collections found"),
    # ---- search._find_function_sync (1) ----
    ("search", "_find_function_sync", "find_function", {"function_name": " "}, lambda t, m: make_ctx(t), "function_name is required"),
    # ---- search.search_hex_pattern (3) ----
    ("search", "search_hex_pattern", "search_hex_pattern", {"pattern": ""}, lambda t, m: make_ctx(t), "pattern is required"),
    ("search", "search_hex_pattern", "search_hex_pattern", {"pattern": "88a1", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("search", "search_hex_pattern", "search_hex_pattern", {"pattern": "88a1"}, lambda t, m: make_ctx(t), "No indexed collections found"),
    # ---- search.find_variable (3) ----
    ("search", "find_variable", "find_variable", {"variable_name": ""}, lambda t, m: make_ctx(t), "variable_name is required"),
    ("search", "find_variable", "find_variable", {"variable_name": "X", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("search", "find_variable", "find_variable", {"variable_name": "X"}, lambda t, m: make_ctx(t), "No indexed collections found"),
    # ---- documents.get_document (2 new + 2 reclassified) ----
    ("documents", "get_document", "get_document", {"file_path": "../x.h", "project": "p"}, lambda t, m: make_ctx(t), "must not contain '..'"),
    ("documents", "get_document", "get_document", {"file_path": "x.h", "project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("documents", "get_document", "get_document", {"file_path": "x.h", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),          # reclassified
    ("documents", "get_document", "get_document", {"file_path": "x.h", "project": "p"},
     lambda t, m: make_ctx(t, collection=_collection([])), "Document 'x.h' not found"),                                                                  # reclassified
    # ---- documents.list_files (1 new + 2 reclassified) ----
    ("documents", "list_files", "list_files", {"project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("documents", "list_files", "list_files", {"project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),                                # reclassified
    ("documents", "list_files", "list_files", {"project": "p"},
     lambda t, m: make_ctx(t, collection=_collection(get=MagicMock(side_effect=RuntimeError("db down")))), "Error querying collection: db down"),        # reclassified
    # ---- documents.get_project_summary (2) ----
    ("documents", "get_project_summary", "get_project_summary", {"project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("documents", "get_project_summary", "get_project_summary", {"project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    # ---- documents._compare_projects_sync (2) ----
    ("documents", "_compare_projects_sync", "compare_projects", {"query": "", "project_a": "a", "project_b": "b"}, lambda t, m: make_ctx(t), "query is required"),
    ("documents", "_compare_projects_sync", "compare_projects", {"query": "q", "project_a": "a", "project_b": ""}, lambda t, m: make_ctx(t), "both project_a and project_b are required"),
    # ---- management._add_project_sync (8) ----
    ("management", "_add_project_sync", "add_project", {"name": " ", "path": "/x"}, lambda t, m: make_ctx(t), "name is required"),
    ("management", "_add_project_sync", "add_project", {"name": "p", "path": ""}, lambda t, m: make_ctx(t), "path is required"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": str(t / "missing")}, lambda t, m: make_ctx(t), "Path does not exist"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": _file(t, "f.txt")}, lambda t, m: make_ctx(t), "Path is not a directory"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "proj", "path": _dir(t, "d")},
     lambda t, m: make_ctx(t, projects=[_project(t)]), "Project 'proj' already exists"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": _dir(t, "empty")}, lambda t, m: make_ctx(t), "No code or doc/spec files found"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": _src_dir(t)},
     lambda t, m: _failing_indexer(make_ctx(t), m), "Indexing failed for project 'p': boom\n  Config rolled back"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": _src_dir(t)},
     lambda t, m: _failing_indexer(make_ctx(t), m, save_fails=True), "rolling back config.yaml failed: ro"),
    # ---- management._add_file_sync (8) ----
    ("management", "_add_file_sync", "add_file", {"file_path": "", "project": "p"}, lambda t, m: make_ctx(t), "file_path is required"),
    ("management", "_add_file_sync", "add_file", {"file_path": "/x.h", "project": " "}, lambda t, m: make_ctx(t), "project is required"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": str(t / "nope.h"), "project": "p"}, lambda t, m: make_ctx(t), "File does not exist"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": _dir(t, "d"), "project": "p"}, lambda t, m: make_ctx(t), "Path is not a file"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": _file(t, "a.h"), "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": _file(t, "a.h"), "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t)]), "has no index"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": _file(t, "proj/a.h"), "project": "proj"},
     lambda t, m: _with(make_ctx(t, projects=[_project(t)], collection=_collection()),
                        lambda c: m.setattr("rag_mcp.file_reader.FileReader.read", lambda self, fp, bp: None)),
     "Could not read file"),
    ("management", "_add_file_sync", "add_file", lambda t: {"file_path": _file(t, "proj/a.h", ""), "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t)], collection=_collection()), "File produced no chunks"),
    # ---- management._add_folder_sync (8) ----
    ("management", "_add_folder_sync", "add_folder", {"folder_path": "", "project": "p"}, lambda t, m: make_ctx(t), "folder_path is required"),
    ("management", "_add_folder_sync", "add_folder", {"folder_path": "/x", "project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": str(t / "nope"), "project": "p"}, lambda t, m: make_ctx(t), "Folder does not exist"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": _file(t, "f.txt"), "project": "p"}, lambda t, m: make_ctx(t), "Path is not a folder"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": _dir(t, "d"), "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": _dir(t, "d"), "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t)]), "has no index"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": _dir(t, "outside"), "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t)], collection=_collection()), "must be inside the project base_path"),
    ("management", "_add_folder_sync", "add_folder", lambda t: {"folder_path": _dir(t, "proj/sub"), "project": "proj", "pattern": "*.zzz"},
     lambda t, m: make_ctx(t, projects=[_project(t)], collection=_collection()), "No files found matching pattern"),
    # ---- management._add_pattern_sync (6) ----
    ("management", "_add_pattern_sync", "add_pattern", {"project": "", "pattern": "*.md"}, lambda t, m: make_ctx(t), "project is required"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "p", "pattern": " "}, lambda t, m: make_ctx(t), "pattern is required"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "p", "pattern": "*.md", "type": "bogus"}, lambda t, m: make_ctx(t), "type must be one of"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "nope", "pattern": "*.md"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "proj", "pattern": "*.md"},
     lambda t, m: make_ctx(t, projects=[_project(t, removed=True)]), "is marked as removed"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "proj", "pattern": "*.md"},
     lambda t, m: make_ctx(t, projects=[_project(t)]), "has no index"),
    # ---- management.remove_project (3) ----
    ("management", "remove_project", "remove_project", {"name": ""}, lambda t, m: make_ctx(t), "name is required"),
    ("management", "remove_project", "remove_project", {"name": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("management", "remove_project", "remove_project", {"name": "proj"},
     lambda t, m: _with(make_ctx(t, projects=[_project(t)], collection=_collection(["a"])),
                        lambda c: setattr(c.loader.save, "side_effect", OSError("ro"))),
     "Collection deleted (1 chunks) but failed to update config.yaml: ro"),
    # ---- management.clear_project_index (3) ----
    ("management", "clear_project_index", "clear_project_index", {"name": " "}, lambda t, m: make_ctx(t), "name is required"),
    ("management", "clear_project_index", "clear_project_index", {"name": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("management", "clear_project_index", "clear_project_index", {"name": "proj"},
     lambda t, m: _with(make_ctx(t, projects=[_project(t)], collection=_collection(["a"])),
                        lambda c: setattr(c.store.delete_collection, "side_effect", RuntimeError("locked"))),
     "Error deleting collection: locked"),
    # ---- management.remove_file_from_index (4) ----
    ("management", "remove_file_from_index", "remove_file_from_index", {"file_path": "", "project": "p"}, lambda t, m: make_ctx(t), "file_path is required"),
    ("management", "remove_file_from_index", "remove_file_from_index", {"file_path": "a.h", "project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("management", "remove_file_from_index", "remove_file_from_index", {"file_path": "a.h", "project": "nope"}, lambda t, m: make_ctx(t), "not found or not indexed"),
    ("management", "remove_file_from_index", "remove_file_from_index", {"file_path": "a.h", "project": "p"},
     lambda t, m: make_ctx(t, collection=_collection(get=MagicMock(side_effect=RuntimeError("db down")))), "Error removing chunks: db down"),
    # ---- logs._search_logs_sync (9) ----
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "severity": "LOUD"}, lambda t, m: make_ctx(t), "Invalid severity 'LOUD'"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "  "}, lambda t, m: make_ctx(t), "At least one of 'query' or a filter"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "x" * 513}, lambda t, m: make_ctx(t), "must not exceed 512"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "error_code_pattern": "a" * 33}, lambda t, m: make_ctx(t), "error_code_pattern must not exceed 32"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "time_range_start": "not-a-time"}, lambda t, m: make_ctx(t), "Error: "),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "time_range_start": "2026-01-02T00:00:00", "time_range_end": "2026-01-01T00:00:00"},
     lambda t, m: make_ctx(t), "time_range_start must not be after time_range_end"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q"}, lambda t, m: make_ctx(t), "No indexed collections found"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "q", "project": "p"},
     lambda t, m: make_ctx(t, collection=_collection(query=MagicMock(side_effect=RuntimeError("db down")))), "search_logs failed for all collections"),
    # ---- logs._index_log_file_sync (7) ----
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": ""}, lambda t, m: make_ctx(t), "project is required"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "nope"}, lambda t, m: make_ctx(t), "Project 'nope' not found"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t, removed=True)]), "is marked as removed"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_project(t)]), "has no log-type sources"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj", "time_from": "14h"},
     lambda t, m: make_ctx(t, projects=[_log_project(t)]), "Invalid time_from format '14h'"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj", "time_to": "xx:yy:zz"},
     lambda t, m: make_ctx(t, projects=[_log_project(t)]), "Invalid time_to format 'xx:yy:zz'"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "nothing.log", "project": "proj"},
     lambda t, m: make_ctx(t, projects=[_log_project(t)]), "No log files matching 'nothing.log'"),
]


def _with(ctx: ToolContext, mutate: Callable[[ToolContext], Any]) -> ToolContext:
    mutate(ctx)
    return ctx


def _src_dir(tmp_path: Path) -> str:
    d = tmp_path / "src"
    d.mkdir(exist_ok=True)
    (d / "main.c").write_text("int main(void) { return 0; }\n")
    return str(d)


def _failing_indexer(ctx: ToolContext, monkeypatch, save_fails: bool = False) -> ToolContext:
    from rag_mcp.indexing_pipeline import IndexingPipeline

    monkeypatch.setattr(IndexingPipeline, "index_project", lambda self, project, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    if save_fails:
        ctx.loader.save.side_effect = [None, OSError("ro")]  # first save (add) ok, rollback save fails
    return ctx


def _zero_chunk_indexer(ctx: ToolContext, monkeypatch) -> ToolContext:
    from rag_mcp.indexing_pipeline import IndexingPipeline

    monkeypatch.setattr(IndexingPipeline, "index_project", lambda self, project, **kw: 0)
    return ctx


def _log_project(tmp_path: Path, with_file: bool = False) -> ProjectConfig:
    project = _project(tmp_path, sources=[SourcePattern(pattern="*.log", type="log")])
    if with_file:
        (Path(project.base_path) / "a.log").write_text("INF 12:00:00:000 something happened\n")
    return project


def _row_id(row: Row) -> str:
    return f"{row[2]}:{row[5][:40]}"


@pytest.mark.parametrize("row", ERROR_PATHS, ids=_row_id)
def test_error_return_emits_one_error_event(row: Row, tmp_path, monkeypatch, capsys):
    module_name, fn_name, tool, kwargs, setup, expected = row
    kwargs = kwargs(tmp_path) if callable(kwargs) else kwargs
    ctx = setup(tmp_path, monkeypatch)
    module = _MODULES[module_name]
    monkeypatch.setattr(module, "_ctx", ctx)
    if module is not search:
        monkeypatch.setattr(search, "_ctx", ctx)  # compare_projects/find_function delegate to search

    text = getattr(module, fn_name)(**kwargs)

    assert isinstance(text, str) and expected in text, text
    recs = _events(capsys, tool)
    assert len(recs) == 1, f"expected exactly one {tool} event, got {len(recs)}"
    rec = recs[0]
    assert rec["outcome"] == "error"
    assert rec["result_count"] == 0
    assert rec["error"] == text[: observability.MAX_ERROR_CHARS] + ("..." if len(text) > observability.MAX_ERROR_CHARS else "")
    assert rec["message"].split(" -> ")[1].startswith("error in ")
    # params are the raw inputs the tool received, for the keys the tool documents
    for key, value in rec["params"].items():
        if key in kwargs and not (key == "query" and len(str(kwargs[key])) > observability.MAX_QUERY_CHARS):
            assert value == kwargs[key], key


def test_error_paths_inventory_is_complete():
    """The plan's inventory: 75 formerly silent early returns + 4 reclassified."""
    assert len(ERROR_PATHS) == 79
    per_tool = {}
    for row in ERROR_PATHS:
        per_tool[row[2]] = per_tool.get(row[2], 0) + 1
    assert per_tool == {
        "search_docs": 5, "find_function": 1, "search_hex_pattern": 3, "find_variable": 3,
        "get_document": 4, "list_files": 3, "get_project_summary": 2, "compare_projects": 2,
        "add_project": 8, "add_file": 8, "add_folder": 8, "add_pattern": 6,
        "remove_project": 3, "clear_project_index": 3, "remove_file_from_index": 4,
        "search_logs": 9, "index_log_file": 7,
    }


def test_error_calls_land_in_counters_not_zero_results(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(documents, "_ctx", make_ctx(tmp_path))
    documents.get_document("x.h", "nope")     # used to be logged as an empty success
    documents.list_files("nope")              # used to be logged as an empty success
    documents.get_project_summary("")         # used to emit nothing

    tc = observability.get_metrics_snapshot()["tool_calls"]
    assert tc["total"] == 3 and tc["errors"] == 3
    for tool in ("get_document", "list_files", "get_project_summary"):
        assert tc["by_tool"][tool] == {**tc["by_tool"][tool], "count": 1, "error_calls": 1, "zero_result_calls": 0}


# ---------------------------------------------------------------------------
# OK paths — empty answers stay ok/0, successes report their count
# ---------------------------------------------------------------------------

OK_PATHS: list[Row] = [
    ("search", "_search_docs_sync", "search_docs", {"query": "q"},
     lambda t, m: make_ctx(t, collection=_collection([]), list_collections=[_meta("rag_proj")]), "No results found"),
    ("search", "search_hex_pattern", "search_hex_pattern", {"pattern": "88a1"},
     lambda t, m: make_ctx(t, collection=_collection([]), list_collections=[_meta("rag_proj")]), "No matches found"),
    ("search", "find_variable", "find_variable", {"variable_name": "X"},
     lambda t, m: make_ctx(t, collection=_collection([]), list_collections=[_meta("rag_proj")]), "No occurrences found"),
    ("documents", "list_projects", "list_projects", {}, lambda t, m: make_ctx(t), "No projects indexed yet"),
    ("logs", "_search_logs_sync", "search_logs", {"query": "nothing", "project": "p"},
     lambda t, m: make_ctx(t, collection=_collection([])), "Found 0 log results"),
    ("documents", "list_files", "list_files", {"project": "p"}, lambda t, m: make_ctx(t, collection=_collection([])), "No files found in project 'p'"),
    ("documents", "get_project_summary", "get_project_summary", {"project": "p"},
     lambda t, m: make_ctx(t, collection=_collection([])), "is indexed but has no chunks"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "proj", "path": _dir(t, "d")},
     lambda t, m: make_ctx(t, projects=[_project(t, removed=True)]), "re-activated"),
    ("management", "_add_project_sync", "add_project", lambda t: {"name": "p", "path": _src_dir(t)},
     lambda t, m: _zero_chunk_indexer(make_ctx(t), m), "added to config but 0 chunks were indexed"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "proj", "pattern": "*.zzz"},
     lambda t, m: make_ctx(t, projects=[_project(t)], collection=_collection()), "Pattern saved to config (no files matched yet)"),
    ("management", "_add_pattern_sync", "add_pattern", {"project": "proj", "pattern": "*.zzz"},
     lambda t, m: make_ctx(t, projects=[_project(t, sources=[SourcePattern("*.zzz", "documentation")])], collection=_collection()),
     "Pattern already exists in config (no files matched)"),
    ("management", "remove_file_from_index", "remove_file_from_index", {"file_path": "a.h", "project": "p"},
     lambda t, m: make_ctx(t, collection=_collection([])), "No chunks found for file 'a.h'"),
    # index_log_file: cancellation and per-file failures are reported in the result text, the call itself is ok (D1)
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj"},
     lambda t, m: _with(make_ctx(t, projects=[_log_project(t, with_file=True)]),
                        lambda c: setattr(c, "indexing_cancelled", lambda: True)),
     "Cancelled before processing a.log"),
    ("logs", "_index_log_file_sync", "index_log_file", {"file": "a.log", "project": "proj"},
     lambda t, m: _with(make_ctx(t, projects=[_log_project(t, with_file=True)]),
                        lambda c: m.setattr(logs, "_process_log_batch", MagicMock(side_effect=RuntimeError("parse boom")))),
     "a.log: error - parse boom"),
]


@pytest.mark.parametrize("row", OK_PATHS, ids=_row_id)
def test_empty_answer_is_ok_with_zero_results_not_an_error(row: Row, tmp_path, monkeypatch, capsys):
    module_name, fn_name, tool, kwargs, setup, expected = row
    kwargs = kwargs(tmp_path) if callable(kwargs) else kwargs
    module = _MODULES[module_name]
    monkeypatch.setattr(module, "_ctx", setup(tmp_path, monkeypatch))

    text = getattr(module, fn_name)(**kwargs)

    assert expected in text, text
    recs = _events(capsys, tool)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok"
    assert recs[0]["result_count"] == 0
    assert "error" not in recs[0]
    t = observability.get_metrics_snapshot()["tool_calls"]["by_tool"][tool]
    assert (t["error_calls"], t["zero_result_calls"]) == (0, 1)


def test_successful_call_reports_count_and_ok(tmp_path, monkeypatch, capsys):
    ctx = make_ctx(tmp_path, collection=_collection(["c1", "c2"]))
    monkeypatch.setattr(documents, "_ctx", ctx)

    text = documents.get_document("x.h", "p")

    assert text.startswith("Document: x.h\nProject: p\nChunks: 2\n")
    recs = _events(capsys, "get_document")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 2
    assert recs[0]["params"] == {"file_path": "x.h", "project": "p"}


def test_params_are_logged_as_received_not_clamped_or_stripped(tmp_path, monkeypatch, capsys):
    """D6: params are captured at entry."""
    monkeypatch.setattr(search, "_ctx", make_ctx(tmp_path))
    search._search_docs_sync("q", project="nope", top_k=500)
    assert _events(capsys, "search_docs")[0]["params"]["top_k"] == 500

    monkeypatch.setattr(management, "_ctx", make_ctx(tmp_path))
    management.remove_file_from_index("  a.h ", " nope ")
    assert _events(capsys, "remove_file_from_index")[0]["params"] == {"file_path": "  a.h ", "project": " nope "}


def test_nested_search_docs_events_are_still_emitted_alongside_the_outer_tool(tmp_path, monkeypatch, capsys):
    """find_function delegates to _search_docs_sync twice; each emits its own
    search_docs event (existing behaviour), plus exactly one find_function event."""
    ctx = make_ctx(tmp_path, collection=_collection([]), list_collections=[_meta("rag_proj")])
    monkeypatch.setattr(search, "_ctx", ctx)

    search._find_function_sync("Foo")

    err = capsys.readouterr().err
    recs = [json.loads(ln) for ln in err.splitlines() if ln.startswith("{")]
    assert [r["tool"] for r in recs] == ["search_docs", "search_docs", "find_function"]
    assert recs[-1]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# logs.py: the paths that only run when a collection / file is actually processed
# (search_logs semantic hits + post-filter, partial collection failure;
#  index_log_file success, no events, time window, cancellation mid-file)
# ---------------------------------------------------------------------------


def _log_hits(error_codes: list[str]) -> dict:
    """collection.query() payload for the semantic path: one hit per error code."""
    n = len(error_codes)
    return {
        "documents": [[f"ERR {c} dispense rejected" for c in error_codes]],
        "metadatas": [[{"file_path": "dev.log", "project": "p", "severity": "error", "error_code": c,
                        "timestamp_range_end": f"2026-01-01T00:00:0{i}"} for i, c in enumerate(error_codes)]],
        "distances": [[0.1 * (i + 1) for i in range(n)]],
    }


def test_search_logs_semantic_hits_with_error_code_prefix_filter(tmp_path, monkeypatch, capsys):
    col = _collection()
    col.query.return_value = _log_hits(["88a153", "7700", "88a200"])
    monkeypatch.setattr(logs, "_ctx", make_ctx(tmp_path, collection=col))

    text = logs._search_logs_sync("dispense", project="p", error_code_pattern="88a", top_k=20)

    assert text.startswith("Found 2 log result(s).")
    assert "88a153" in text and "88a200" in text and "7700" not in text
    # over-fetch for the post-filter: top_k * 3
    assert col.query.call_args.kwargs["n_results"] == 60
    recs = _events(capsys, "search_logs")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 2
    assert recs[0]["params"] == {"query": "dispense", "project": "p", "severity": "", "top_k": 20}


def test_search_logs_partial_collection_failure_is_ok_not_error(tmp_path, monkeypatch, capsys):
    """One collection cannot be opened, one raises on query, one answers:
    the call succeeds with the answers it got (only an all-failed run is an error)."""
    good = _collection(); good.query.return_value = _log_hits(["88a153"])
    bad = _collection(); bad.query.side_effect = RuntimeError("db down")
    ctx = make_ctx(tmp_path, list_collections=[_meta("rag_unopenable"), _meta("rag_bad"), _meta("rag_good")])
    ctx.store.client.get_collection.side_effect = lambda name: (
        (_ for _ in ()).throw(RuntimeError("gone")) if name == "rag_unopenable" else {"rag_bad": bad, "rag_good": good}[name]
    )
    monkeypatch.setattr(logs, "_ctx", ctx)

    text = logs._search_logs_sync("dispense")

    assert text.startswith("Found 1 log result(s).")
    recs = _events(capsys, "search_logs")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 1


def _index_log_ctx(tmp_path, monkeypatch, batch_result=(3, 5), lines=("INF 14:30:00:000 something happened",)):
    """Context for index_log_file with a one-file log project; the per-batch
    pipeline is replaced so the test is about the tool's control flow and event."""
    project = _log_project(tmp_path, with_file=True)
    (Path(project.base_path) / "a.log").write_text("".join(ln + "\n" for ln in lines))
    batch = MagicMock(return_value=batch_result)
    monkeypatch.setattr(logs, "_process_log_batch", batch)
    ctx = make_ctx(tmp_path, projects=[project])
    monkeypatch.setattr(logs, "_ctx", ctx)
    return ctx, batch


def test_index_log_file_success_reports_chunks(tmp_path, monkeypatch, capsys):
    _index_log_ctx(tmp_path, monkeypatch, batch_result=(3, 5))

    text = logs._index_log_file_sync("a.log", "proj")

    assert text.startswith("Log indexing complete: 3 chunks created in ")
    assert "\u2713 a.log: 5 events \u2192 3 chunks in " in text
    assert "Time range:" not in text
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 3
    assert recs[0]["params"] == {"file": "a.log", "project": "proj", "time_from": "", "time_to": ""}


def test_index_log_file_file_without_events_is_ok_with_zero_chunks(tmp_path, monkeypatch, capsys):
    _index_log_ctx(tmp_path, monkeypatch, batch_result=(0, 0))

    text = logs._index_log_file_sync("a.log", "proj")

    assert text.startswith("Log indexing complete: 0 chunks created in ")
    assert "\u2717 a.log: no indexable events" in text
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 0


def test_index_log_file_time_window_stops_reading_past_time_to(tmp_path, monkeypatch, capsys):
    ctx, batch = _index_log_ctx(
        tmp_path, monkeypatch, batch_result=(2, 2),
        lines=("INF:14:30:00:000 in window", "INF:16:00:00:000 past the window", "INF:16:00:01:000 never read"),
    )

    text = logs._index_log_file_sync("a.log", "proj", time_from="14:00:00", time_to="15:30:00")

    # the fast early-stop (device format ``XXX:HH:MM:SS:mmm``) only handed the in-window line to the pipeline
    assert batch.call_count == 1
    assert batch.call_args.args[0] == ["INF:14:30:00:000 in window"]
    assert batch.call_args.args[9:11] == ("14:00:00.000", "15:30:00.000")  # parsed_from, parsed_to
    assert "Time range: 14:00:00 \u2192 15:30:00" in text
    assert "\u2713 a.log: 2 events \u2192 2 chunks in " in text and "(time: 14:00:00 \u2192 15:30:00)" in text
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 2
    assert recs[0]["params"] == {"file": "a.log", "project": "proj", "time_from": "14:00:00", "time_to": "15:30:00"}


def test_index_log_file_cancelled_after_partial_progress(tmp_path, monkeypatch, capsys):
    """Cancellation flagged after the file's last batch was stored: the call is
    still ``ok`` (D1) and the summary line says what was preserved.

    Note (pre-existing, not changed here): the ``break`` on cancellation skips
    ``total_chunks += file_chunks``, so the headline and the event report 0
    chunks even though the summary says 3 were stored.
    """
    ctx, _ = _index_log_ctx(tmp_path, monkeypatch, batch_result=(3, 5))
    # checked (1) before the file, (2) before the remaining batch, (3) after it
    ctx.indexing_cancelled = MagicMock(side_effect=[False, False, True])

    text = logs._index_log_file_sync("a.log", "proj")

    assert "\u26a0 a.log: cancelled after batch 0 (3 chunks stored, preserved)" in text
    assert text.startswith("Log indexing complete: 0 chunks created in ")
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok"
    assert recs[0]["result_count"] == 0
    assert "error" not in recs[0]


def test_search_logs_filter_only_with_device_and_event_type(tmp_path, monkeypatch, capsys):
    col = _collection([])
    monkeypatch.setattr(logs, "_ctx", make_ctx(tmp_path, collection=col))

    text = logs._search_logs_sync(project="p", severity="ERROR", device_id="dev-1", event_type="command")

    assert text.startswith("Found 0 log results.")
    where = col.get.call_args.kwargs["where"]
    assert {"device_id": "dev-1"} in where["$and"] and {"event_type": "command"} in where["$and"]
    assert {"severity": "error"} in where["$and"] and {"file_type": "log"} in where["$and"]
    assert not col.query.called, "no query text -> filter-only path uses get()"
    recs = _events(capsys, "search_logs")
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 0


def test_index_log_file_multi_batch_file_sums_batches(tmp_path, monkeypatch, capsys):
    """> 10k lines: one full batch inside the read loop plus the remainder after it."""
    _, batch = _index_log_ctx(tmp_path, monkeypatch, batch_result=(2, 4), lines=[f"line {i}" for i in range(10_001)])

    text = logs._index_log_file_sync("a.log", "proj")

    assert batch.call_count == 2
    assert len(batch.call_args_list[0].args[0]) == 10_000 and len(batch.call_args_list[1].args[0]) == 1
    assert batch.call_args_list[1].args[11] == 2, "second batch starts at the chunk offset of the first"
    assert "\u2713 a.log: 8 events \u2192 4 chunks in " in text
    recs = _events(capsys, "index_log_file")  # free-text progress lines on stderr are ignored by _events
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 4


def test_index_log_file_cancelled_at_batch_boundary_stores_nothing_more(tmp_path, monkeypatch, capsys):
    ctx, batch = _index_log_ctx(tmp_path, monkeypatch, batch_result=(2, 4), lines=[f"line {i}" for i in range(10_001)])
    # (1) before the file: go; (2) at the 10k-line batch boundary: cancelled; later checks: cancelled
    ctx.indexing_cancelled = MagicMock(side_effect=[False, True, True, True])

    text = logs._index_log_file_sync("a.log", "proj")

    assert batch.call_count == 0, "the full batch is not processed once cancellation is seen"
    assert "\u26a0 a.log: cancelled after batch 0 (0 chunks stored, preserved)" in text
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 0


# --- the real per-batch pipeline (parse -> filter -> group -> embed -> upsert), mocked store/model ---

_LOG_LINES = [
    "2026-01-01T14:30:00 ERROR dispense rejected 88a153",
    "2026-01-01T14:30:00.100 INFO retry scheduled",
    "2026-01-01T16:00:00 INFO all good",
]


def _real_batch_components(filename: str = "a.log"):
    from rag_mcp.log.parsing.config_models import LogSettings
    from rag_mcp.log.parsing.content_transform import ContentTransform
    from rag_mcp.log.parsing.event_grouper import EventGrouper
    from rag_mcp.log.parsing.line_filter import LineFilter
    from rag_mcp.log.parsing.log_parser import LogParser

    settings = LogSettings()
    parser = LogParser(
        patterns=None, settings=settings, severity_mapping=None,
        line_filter=LineFilter(filters=[], default_action="include"), filename=filename,
    )
    return parser, ContentTransform(transforms=[]), EventGrouper(settings=settings, grouping_rules=None)


def _embedder() -> MagicMock:
    emb = MagicMock()
    emb.encode.side_effect = lambda texts: [[0.1, 0.2]] * len(texts)
    return emb


def test_process_log_batch_parses_groups_embeds_and_upserts():
    parser, transform, grouper = _real_batch_components()
    emb, col, source = _embedder(), MagicMock(), MagicMock(description="device logs")

    chunks, events = logs._process_log_batch(
        _LOG_LINES, parser, transform, grouper, emb, col, "a.log", source, "proj", None, None, 0,
    )

    assert (chunks, events) == (3, 3)
    assert emb.encode.call_count == 1 and len(emb.encode.call_args.args[0]) == 3
    assert col.upsert.call_count == 1
    kw = col.upsert.call_args.kwargs
    assert kw["ids"] == ["a.log::line_1", "a.log::line_2", "a.log::line_3"]  # deterministic, resumable
    assert len(kw["embeddings"]) == len(kw["documents"]) == len(kw["metadatas"]) == 3
    first = kw["metadatas"][0]
    assert first["file_type"] == "log" and first["project"] == "proj" and first["file_path"] == "a.log"
    assert first["severity"] == "error" and first["source_description"] == "device logs"
    assert (first["line_start"], first["line_end"], first["chunk_index"]) == (1, 1, 0)
    assert isinstance(first["ts_start_epoch"], float) and isinstance(first["ts_end_epoch"], float)
    assert "88a153" in kw["documents"][0]


def test_process_log_batch_time_window_drops_events_outside_it_and_offsets_chunk_index():
    parser, transform, grouper = _real_batch_components()
    emb, col, source = _embedder(), MagicMock(), MagicMock(description="d")

    chunks, events = logs._process_log_batch(
        _LOG_LINES, parser, transform, grouper, emb, col, "a.log", source, "proj",
        "14:00:00.000", "15:30:00.000", 10,
    )

    assert (chunks, events) == (2, 2), "the 16:00 event is outside the window"
    kw = col.upsert.call_args.kwargs
    assert kw["ids"] == ["a.log::line_1", "a.log::line_2"]
    assert [m["chunk_index"] for m in kw["metadatas"]] == [10, 11]


def test_process_log_batch_with_nothing_parseable_stores_nothing():
    parser, transform, grouper = _real_batch_components()
    emb, col = _embedder(), MagicMock()

    assert logs._process_log_batch(["no timestamp here", ""], parser, transform, grouper, emb, col,
                                   "a.log", MagicMock(), "proj", None, None, 0) == (0, 0)
    assert not emb.encode.called and not col.upsert.called


def test_index_log_file_end_to_end_with_real_pipeline(tmp_path, monkeypatch, capsys):
    """No batch mock: the tool drives the real parser/grouper over a real file;
    only the embedding model and ChromaDB are mocked."""
    project = _log_project(tmp_path, with_file=True)
    (Path(project.base_path) / "a.log").write_text("".join(ln + "\n" for ln in _LOG_LINES))
    ctx = make_ctx(tmp_path, projects=[project])
    ctx.embedding_gen = _embedder()
    col = MagicMock()
    ctx.store.get_or_create_collection.return_value = col
    monkeypatch.setattr(logs, "_ctx", ctx)

    text = logs._index_log_file_sync("a.log", "proj", time_from="14:00:00", time_to="15:30:00")

    assert text.startswith("Log indexing complete: 2 chunks created in ")
    assert "\u2713 a.log: 2 events \u2192 2 chunks in " in text
    assert col.upsert.call_args.kwargs["ids"] == ["a.log::line_1", "a.log::line_2"]
    ctx.store.get_or_create_collection.assert_called_once_with("proj", "d")
    recs = _events(capsys, "index_log_file")
    assert len(recs) == 1
    assert recs[0]["outcome"] == "ok" and recs[0]["result_count"] == 2


def test_search_specs_and_search_code_are_attributed_to_search_docs(tmp_path, monkeypatch, capsys):
    """They are thin wrappers over the search_docs implementation and emit no
    event of their own — documented in OBSERVABILITY.md ("Nesting")."""
    ctx = make_ctx(tmp_path, collection=_collection([]), list_collections=[_meta("rag_proj")])
    monkeypatch.setattr(search, "_ctx", ctx)

    search._search_docs_sync("q", file_type="documentation")   # what search_specs delegates to
    search._search_code_sync("q")                              # headers + sources
    search._search_code_sync("", headers_only=True)            # invalid input through the wrapper

    recs = [json.loads(ln) for ln in capsys.readouterr().err.splitlines() if ln.startswith("{")]
    assert {r["tool"] for r in recs} == {"search_docs"}, "no search_specs / search_code events"
    assert len(recs) == 4, "1 (specs) + 2 (code) + 1 (code, headers_only)"
    assert [r["outcome"] for r in recs] == ["ok", "ok", "ok", "error"]
    by_tool = observability.get_metrics_snapshot()["tool_calls"]["by_tool"]
    assert set(by_tool) == {"search_docs"}
    assert by_tool["search_docs"]["error_calls"] == 1, "failures through the wrappers stay visible"


# ---------------------------------------------------------------------------
# Static guard: no return path can bypass ToolCall
# ---------------------------------------------------------------------------

# Functions that legitimately return without emitting their own event.
_GUARD_ALLOWLIST = {
    ("search", "_search_code_sync"),   # pure delegation to _search_docs_sync (documented nesting)
}


def _registered_tool_bodies() -> dict[tuple[str, str], ast.FunctionDef]:
    """Every function registered via ``mcp.tool()(name)`` in the four tool
    modules, resolved to its sync body (``_<name>_sync``) when it is an
    async wrapper."""
    bodies = {}
    for mod_name in _MODULES:
        tree = ast.parse((_SRC_DIR / "rag_mcp" / "tools" / f"{mod_name}.py").read_text(encoding="utf-8"))
        funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        register = funcs["register"]
        registered = [
            n.args[0].id
            for n in ast.walk(register)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Call)
            and getattr(n.func.func, "attr", None) == "tool" and n.args and isinstance(n.args[0], ast.Name)
        ]
        assert registered, f"no mcp.tool()(...) registrations found in {mod_name}.py"
        for name in registered:
            fn = funcs[name]
            if isinstance(fn, ast.AsyncFunctionDef):
                # `return await anyio.to_thread.run_sync(lambda: _x_sync(...))`:
                # the body is the module-level function the lambda calls.
                # (search_specs delegates straight to _search_docs_sync.)
                called = [
                    n.func.id for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs
                ]
                assert len(called) == 1, f"{mod_name}.{name}: cannot resolve sync body ({called})"
                fn = funcs[called[0]]
            bodies[(mod_name, fn.name)] = fn
    return bodies


def _own_returns(fn: ast.FunctionDef) -> list[ast.Return]:
    """Return statements of ``fn`` itself, not of nested defs/classes
    (``_parse_time`` inside index_log_file, ``SilentConsole`` inside add_project)."""
    out: list[ast.Return] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                out.append(child)
            visit(child)

    visit(fn)
    return out


def _is_call_ok_or_error(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "call"
        and node.func.attr in {"ok", "error"}
    )


def test_every_return_in_a_tool_body_goes_through_toolcall():
    bodies = _registered_tool_bodies()
    # 20 registered tools (6 search + 5 documents + 7 management + 2 logs) resolve to 19
    # distinct bodies: search_specs and search_docs share _search_docs_sync.
    assert len(bodies) == 19, sorted(bodies)

    offenders = []
    for (mod_name, fn_name), fn in bodies.items():
        if (mod_name, fn_name) in _GUARD_ALLOWLIST:
            continue
        first = fn.body[0]
        while isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):  # skip docstring
            first = fn.body[fn.body.index(first) + 1]
        # the body must start by binding `call = ToolCall(...)`
        starts_with_toolcall = (
            isinstance(first, ast.Assign)
            and len(first.targets) == 1
            and getattr(first.targets[0], "id", None) == "call"
            and isinstance(first.value, ast.Call)
            and getattr(first.value.func, "id", None) == "ToolCall"
        )
        if not starts_with_toolcall:
            # add_pattern / index_log_file start with a local ``import glob``; allow one leading import
            nxt = fn.body[fn.body.index(first) + 1] if isinstance(first, (ast.Import, ast.ImportFrom)) else None
            starts_with_toolcall = (
                isinstance(nxt, ast.Assign) and getattr(nxt.targets[0], "id", None) == "call"
            )
        if not starts_with_toolcall:
            offenders.append(f"{mod_name}.{fn_name}: body does not start with `call = ToolCall(...)`")
        for ret in _own_returns(fn):
            if not _is_call_ok_or_error(ret.value):
                offenders.append(f"{mod_name}.{fn_name}:{ret.lineno}: return does not go through call.ok()/call.error()")
    assert not offenders, "\n".join(offenders)


def test_guard_allowlist_is_minimal():
    """Only functions that really exist and really delegate are allow-listed."""
    bodies = _registered_tool_bodies()
    for key in _GUARD_ALLOWLIST:
        assert key in bodies, key
        src_returns = _own_returns(bodies[key])
        assert src_returns and not any(_is_call_ok_or_error(r.value) for r in src_returns)
