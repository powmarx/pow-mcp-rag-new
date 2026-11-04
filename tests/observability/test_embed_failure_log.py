"""Tests for structured ``embed_failure`` JSON logging.

``EmbeddingGenerator.encode()`` / ``encode_query()`` are the single choke
point for every embedding in the code base. On failure they must:
  * emit exactly one ``embed_failure`` JSON line (level ERROR) on stderr and
    append the identical line to the JSONL file,
  * re-raise the ORIGINAL exception unchanged, so every caller's existing
    error handling (and its existing stderr text) is untouched.

No ChromaDB or real model is needed: the SentenceTransformer instance is a
MagicMock whose ``.encode`` raises. The "through real callers" tests drive the
actual IndexingPipeline, LogIndexer and search_docs code paths.
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
from rag_mcp.embedding_generator import EmbeddingGenerator

REQUIRED_KEYS = {"ts", "level", "event", "pid", "message", "kind", "batch_size", "model", "error_type", "error"}


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


def _failing_generator(exc: BaseException, model_name: str = "BAAI/bge-m3") -> EmbeddingGenerator:
    gen = EmbeddingGenerator(model_name, query_instruction="Represent this: ")
    gen.model = MagicMock()
    gen.model.encode.side_effect = exc
    return gen


def _working_generator(dim: int = 4) -> EmbeddingGenerator:
    gen = EmbeddingGenerator("m")
    gen.model = MagicMock()
    gen.model.encode.side_effect = lambda texts, **kw: MagicMock(tolist=lambda: [[0.0] * dim for _ in texts])
    return gen


def _stderr_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def _json_lines(lines: list[str]) -> list[dict]:
    return [json.loads(ln) for ln in lines if ln.startswith("{")]


def _embed_failures(records: list[dict]) -> list[dict]:
    return [r for r in records if r["event"] == "embed_failure"]


def _file_records(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# EmbeddingGenerator contract
# ---------------------------------------------------------------------------


def test_encode_failure_emits_one_error_event_and_reraises_same_exception(capsys, log_file):
    boom = RuntimeError("CUDA out of memory")
    gen = _failing_generator(boom)

    with pytest.raises(RuntimeError) as excinfo:
        gen.encode(["a", "b", "c"])
    assert excinfo.value is boom, "the original exception object must propagate"

    recs = _embed_failures(_json_lines(_stderr_lines(capsys)))
    assert len(recs) == 1
    rec = recs[0]
    assert REQUIRED_KEYS <= rec.keys()
    assert rec["level"] == "ERROR"
    assert rec["kind"] == "documents"
    assert rec["batch_size"] == 3
    assert rec["model"] == "BAAI/bge-m3"
    assert rec["error_type"] == "RuntimeError"
    assert rec["error"] == "CUDA out of memory"
    assert rec["message"] == (
        "embedding failed (kind=documents, batch_size=3, model=BAAI/bge-m3): "
        "RuntimeError: CUDA out of memory"
    )
    assert _file_records(log_file) == recs, "same line appended to the JSONL file"


def test_encode_query_failure_is_tagged_as_query(capsys):
    gen = _failing_generator(ValueError("bad input"))
    with pytest.raises(ValueError):
        gen.encode_query("how does dispense work")

    rec = _embed_failures(_json_lines(_stderr_lines(capsys)))[0]
    assert rec["kind"] == "query"
    assert rec["batch_size"] == 1
    assert rec["error_type"] == "ValueError"
    # No file/project context by design (option A), and no query text leaked
    assert "context" not in rec
    assert "dispense" not in json.dumps(rec)


def test_model_not_loaded_is_an_embed_failure_too(capsys):
    gen = EmbeddingGenerator("m")  # .model is None
    with pytest.raises(RuntimeError, match="Model not loaded"):
        gen.encode(["x"])
    rec = _embed_failures(_json_lines(_stderr_lines(capsys)))[0]
    assert rec["error_type"] == "RuntimeError"
    assert rec["error"].startswith("Model not loaded")


def test_successful_encode_emits_nothing(capsys, log_file):
    gen = _working_generator()
    out = gen.encode(["a", "b"])
    assert out == [[0.0] * 4, [0.0] * 4]
    assert gen.encode_query("q") == [0.0] * 4
    assert _stderr_lines(capsys) == []
    assert not log_file.exists()


def test_error_message_is_truncated_to_500_chars(capsys):
    gen = _failing_generator(RuntimeError("e" * 2000))
    with pytest.raises(RuntimeError):
        gen.encode(["x"])
    rec = _embed_failures(_json_lines(_stderr_lines(capsys)))[0]
    assert rec["error"] == "e" * 500 + "..."
    assert len(rec["message"]) < 700


def test_non_exception_subclass_errors_are_reraised(capsys):
    """KeyboardInterrupt/SystemExit are not logged and still propagate."""
    gen = _failing_generator(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        gen.encode(["x"])
    assert _embed_failures(_json_lines(_stderr_lines(capsys))) == []


def test_each_failing_batch_is_its_own_event(capsys, log_file):
    gen = _failing_generator(RuntimeError("boom"))
    for n in (1, 5, 10):
        with pytest.raises(RuntimeError):
            gen.encode(["x"] * n)
    assert [r["batch_size"] for r in _file_records(log_file)] == [1, 5, 10]


# ---------------------------------------------------------------------------
# Through real callers: existing error handling and stderr text unchanged
# ---------------------------------------------------------------------------


def test_indexing_pipeline_outer_message_unchanged_and_event_emitted(tmp_path, capsys, log_file):
    """Mirrors tests/indexing/test_indexing_pipeline.py::test_one_failing_file_does_not_abort_project
    but with a REAL EmbeddingGenerator so the event fires."""
    from rag_mcp.chunker import Chunk
    from rag_mcp.config_loader import (
        AppConfig, ChunkingConfig, EmbeddingConfig, ProjectConfig, SourcePattern, StorageConfig,
    )
    from rag_mcp.file_reader import FileContent
    from rag_mcp.indexing_pipeline import IndexingPipeline

    (tmp_path / "bad.md").write_text("bad file")
    (tmp_path / "good.md").write_text("good file")

    gen = EmbeddingGenerator("m")
    gen.model = MagicMock()

    def model_encode(texts, **kw):
        if texts == ["bad file"]:
            raise RuntimeError("boom")
        return MagicMock(tolist=lambda: [[0.0] * 4 for _ in texts])

    gen.model.encode.side_effect = model_encode

    store = MagicMock()
    store.get_existing_hash.return_value = "old-hash"
    chunker = MagicMock()
    chunker.chunk.side_effect = lambda text: [Chunk(content=text, index=0, total=1)]
    file_reader = MagicMock(pdf_cache_dir=tmp_path / "pdf_cache")
    file_reader.read.side_effect = lambda p, base: FileContent(
        path=p, relative_path=p.name, content=p.read_text(), file_hash=f"hash-{p.name}"
    )
    config = AppConfig(
        embedding=EmbeddingConfig(), storage=StorageConfig(path=str(tmp_path)),
        chunking=ChunkingConfig(), projects=[],
    )
    pipeline = IndexingPipeline(
        config=config, file_reader=file_reader, chunker=chunker,
        embedding_gen=gen, store=store, console=MagicMock(),
    )
    project = ProjectConfig(
        name="proj", description="", base_path=str(tmp_path),
        sources=[SourcePattern(pattern="*.md", type="documentation")],
    )

    total = pipeline.index_project(project)

    assert total == 1  # good.md still indexed: control flow unchanged
    lines = _stderr_lines(capsys)
    # Existing free-text line from index_project's catch-all is still there, verbatim
    assert any("[reindex] proj: failed to index bad.md: RuntimeError: boom" in ln for ln in lines)
    # ...plus exactly one structured event
    recs = _embed_failures(_json_lines(lines))
    assert len(recs) == 1 and recs[0]["kind"] == "documents" and recs[0]["batch_size"] == 1
    assert _file_records(log_file) == recs


def test_search_docs_error_string_unchanged_and_event_emitted(capsys, log_file, monkeypatch):
    from rag_mcp.tools import search as search_mod

    ctx = MagicMock()
    ctx.embedding_gen = _failing_generator(RuntimeError("model exploded"))
    monkeypatch.setattr(search_mod, "_ctx", ctx)

    out = search_mod._search_docs_sync("how does dispense work")

    assert out == "Error encoding query: model exploded"  # existing behaviour
    all_recs = _json_lines(_stderr_lines(capsys))
    recs = _embed_failures(all_recs)
    assert len(recs) == 1 and recs[0]["kind"] == "query"
    assert _embed_failures(_file_records(log_file)) == recs
    # the tool's own early return is now observable too: one tool_call with outcome=error
    tool_recs = [r for r in all_recs if r["event"] == "tool_call"]
    assert len(tool_recs) == 1
    assert tool_recs[0]["tool"] == "search_docs"
    assert tool_recs[0]["outcome"] == "error"
    assert tool_recs[0]["error"] == out


def test_log_indexer_incremental_keeps_offset_and_emits_event(capsys, log_file):
    from rag_mcp.log.log_indexer import LogIndexer

    indexer = LogIndexer(
        log_parser=MagicMock(), content_transform=MagicMock(), event_grouper=MagicMock(),
        embedding_gen=_failing_generator(RuntimeError("boom")), store=MagicMock(),
    )
    group = MagicMock(text="ERR 88a153 dispense rejected")
    indexer._pipeline = MagicMock()
    indexer._pipeline.process.return_value = [group, group]
    collection = MagicMock()
    collection.get.return_value = {"ids": ["__offset__device.log"], "metadatas": [{"byte_offset": 10}]}

    content = b"0123456789ERR 88a153 dispense rejected\n"
    chunks, offset, err = indexer.index_file_incremental(
        collection, "device.log", content, "proj", "desc"
    )

    # Existing contract: nothing stored, previous offset retained, error string returned
    assert (chunks, offset) == (0, 10)
    assert err == "Failed to index 'device.log' from offset 10: boom"
    collection.upsert.assert_not_called()
    lines = _stderr_lines(capsys)
    assert any("[error] Failed to index 'device.log' from offset 10: boom" in ln for ln in lines)
    recs = _embed_failures(_json_lines(lines))
    assert len(recs) == 1 and recs[0]["batch_size"] == 2
    assert _file_records(log_file) == recs


def test_tool_call_and_embed_failure_share_one_file(capsys, log_file):
    from rag_mcp.tools.helpers import log_tool_call

    log_tool_call("search_docs", {"query": "a"}, time.time(), 0)
    with pytest.raises(RuntimeError):
        _failing_generator(RuntimeError("boom")).encode(["x"])
    log_tool_call("search_docs", {"query": "b"}, time.time(), 0)

    assert [r["event"] for r in _file_records(log_file)] == ["tool_call", "embed_failure", "tool_call"]
