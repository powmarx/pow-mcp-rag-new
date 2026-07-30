"""
Unit tests for cross-encoder reranking in search_docs.

Covers:
- Reranking reorders results by cross-encoder score (not just bi-encoder distance)
- Reranking disabled in config -> falls back to vector-search order
- Reranker failure -> falls back gracefully to vector-search order (no crash)
- RerankerConfig defaults and YAML round-trip
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.config_loader import (
    AppConfig,
    ChunkingConfig,
    ConfigLoader,
    RerankerConfig,
    StorageConfig,
)


# ---------------------------------------------------------------------------
# RerankerConfig parsing / defaults
# ---------------------------------------------------------------------------

def test_reranker_config_defaults():
    """RerankerConfig should default to enabled with the ms-marco cross-encoder."""
    cfg = RerankerConfig()
    assert cfg.enabled is True
    assert cfg.model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert cfg.overfetch_factor == 4


def test_reranker_config_loaded_from_yaml():
    """reranker: section in config.yaml should be parsed into RerankerConfig."""
    content = """
embedding:
  model: "BAAI/bge-small-en-v1.5"
reranker:
  enabled: false
  model: "some/other-cross-encoder"
  overfetch_factor: 6
storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
chunking:
  chunk_size: 500
  chunk_overlap: 100
projects: []
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    try:
        config = ConfigLoader(path).load()
        assert config.reranker.enabled is False
        assert config.reranker.model == "some/other-cross-encoder"
        assert config.reranker.overfetch_factor == 6
    finally:
        path.unlink()


def test_reranker_config_defaults_when_section_missing():
    """Config without a reranker: section should fall back to RerankerConfig defaults."""
    content = """
embedding:
  model: "BAAI/bge-small-en-v1.5"
storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
chunking:
  chunk_size: 500
  chunk_overlap: 100
projects: []
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    try:
        config = ConfigLoader(path).load()
        assert config.reranker.enabled is True
        assert config.reranker.model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    finally:
        path.unlink()


def test_reranker_config_save_and_reload():
    """Saving config should round-trip the reranker section."""
    content = """
embedding:
  model: "BAAI/bge-small-en-v1.5"
reranker:
  enabled: true
  model: "cross-encoder/ms-marco-MiniLM-L-6-v2"
  overfetch_factor: 4
storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
chunking:
  chunk_size: 500
  chunk_overlap: 100
projects: []
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    try:
        loader = ConfigLoader(path)
        config = loader.load()
        config.reranker.overfetch_factor = 8
        loader.save(config)

        reloaded = loader.load()
        assert reloaded.reranker.overfetch_factor == 8
        assert reloaded.reranker.enabled is True
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# Reranker wrapper class
# ---------------------------------------------------------------------------

def test_reranker_rerank_requires_loaded_model():
    """Calling rerank() before load() should raise a clear error."""
    from rag_mcp.reranker import Reranker

    r = Reranker("cross-encoder/ms-marco-MiniLM-L-6-v2")
    with pytest.raises(RuntimeError, match="not loaded"):
        r.rerank("query", ["doc1", "doc2"])


def test_reranker_rerank_empty_documents():
    """rerank() with an empty document list should return an empty list without
    requiring the model to score anything."""
    from rag_mcp.reranker import Reranker

    r = Reranker("cross-encoder/ms-marco-MiniLM-L-6-v2")
    r.model = MagicMock()  # pretend it's loaded
    assert r.rerank("query", []) == []
    r.model.predict.assert_not_called()


# ---------------------------------------------------------------------------
# search_docs integration with a mocked reranker
# ---------------------------------------------------------------------------

def _make_search_ctx(tmp_path: Path, reranker_enabled: bool, reranker_mock=None):
    """Build a ToolContext with a fake ChromaDB collection and mocked embedding/reranker."""
    from rag_mcp.tools import ToolContext

    config = AppConfig(
        projects=[],
        storage=StorageConfig(path=str(tmp_path / "data"), collection_prefix="rag"),
        chunking=ChunkingConfig(chunk_size=200, chunk_overlap=20, separators=["\n\n", "\n"]),
        embedding=MagicMock(),
        reranker=RerankerConfig(enabled=reranker_enabled, overfetch_factor=4),
    )

    # Three fake bi-encoder results, deliberately in an order that reranking
    # should invert (the vector search "winner" is the cross-encoder loser).
    fake_query_results = {
        "documents": [["doc about apples", "doc about oranges", "doc about bananas"]],
        "metadatas": [[
            {"file_path": "a.md", "project": "p", "file_type": "documentation"},
            {"file_path": "b.md", "project": "p", "file_type": "documentation"},
            {"file_path": "c.md", "project": "p", "file_type": "documentation"},
        ]],
        "distances": [[0.1, 0.2, 0.3]],  # relevance 0.9, 0.8, 0.7 (bi-encoder order: a,b,c)
    }

    mock_collection = MagicMock()
    mock_collection.query.return_value = fake_query_results

    mock_store = MagicMock()
    mock_store.get_collection.return_value = mock_collection

    mock_embedding_gen = MagicMock()
    mock_embedding_gen.encode_query.return_value = [0.1] * 384

    ctx = ToolContext(
        config=config,
        loader=MagicMock(),
        store=mock_store,
        embedding_gen=mock_embedding_gen,
        ensure_model_loaded=MagicMock(),
        reindex_in_progress=lambda: False,
        indexing_cancelled=lambda: False,
        set_indexing_cancelled=MagicMock(),
        reranker=reranker_mock,
        ensure_reranker_loaded=MagicMock(),
    )
    return ctx


def test_search_docs_reranks_when_enabled(tmp_path, monkeypatch):
    """When reranking is enabled, results should be reordered by cross-encoder score,
    not by the original bi-encoder distance ordering."""
    from rag_mcp.reranker import Reranker

    mock_reranker = MagicMock(spec=Reranker)
    # Cross-encoder scores the bi-encoder's 3rd result ("bananas") as most relevant.
    mock_reranker.rerank.return_value = [0.1, 0.5, 0.9]

    ctx = _make_search_ctx(tmp_path, reranker_enabled=True, reranker_mock=mock_reranker)

    import rag_mcp.tools.search as search_mod
    monkeypatch.setattr(search_mod, "_ctx", ctx)

    result = search_mod._search_docs_sync("fruit query", project="p", top_k=3)

    mock_reranker.rerank.assert_called_once()
    # "bananas" (c.md) should now be ranked first despite being 3rd in vector search.
    first_result_pos = result.find("c.md")
    second_result_pos = result.find("b.md")
    third_result_pos = result.find("a.md")
    assert first_result_pos != -1 and second_result_pos != -1 and third_result_pos != -1
    assert first_result_pos < second_result_pos < third_result_pos


def test_search_docs_skips_reranking_when_disabled(tmp_path, monkeypatch):
    """When reranking is disabled in config, results should keep the original
    bi-encoder distance order and the reranker should never be invoked."""
    from rag_mcp.reranker import Reranker

    mock_reranker = MagicMock(spec=Reranker)
    ctx = _make_search_ctx(tmp_path, reranker_enabled=False, reranker_mock=mock_reranker)

    import rag_mcp.tools.search as search_mod
    monkeypatch.setattr(search_mod, "_ctx", ctx)

    result = search_mod._search_docs_sync("fruit query", project="p", top_k=3)

    mock_reranker.rerank.assert_not_called()
    # Original vector-search order preserved: a.md, b.md, c.md
    pos_a = result.find("a.md")
    pos_b = result.find("b.md")
    pos_c = result.find("c.md")
    assert pos_a < pos_b < pos_c


def test_search_docs_falls_back_when_reranker_raises(tmp_path, monkeypatch):
    """If the cross-encoder throws during scoring, search_docs should fall back
    to the bi-encoder ordering instead of failing the whole search."""
    from rag_mcp.reranker import Reranker

    mock_reranker = MagicMock(spec=Reranker)
    mock_reranker.rerank.side_effect = RuntimeError("model exploded")

    ctx = _make_search_ctx(tmp_path, reranker_enabled=True, reranker_mock=mock_reranker)

    import rag_mcp.tools.search as search_mod
    monkeypatch.setattr(search_mod, "_ctx", ctx)

    result = search_mod._search_docs_sync("fruit query", project="p", top_k=3)

    # Should not raise, and should still return the 3 (fallback-ordered) results.
    assert "Found 3 results" in result
    pos_a = result.find("a.md")
    pos_b = result.find("b.md")
    pos_c = result.find("c.md")
    assert pos_a < pos_b < pos_c
