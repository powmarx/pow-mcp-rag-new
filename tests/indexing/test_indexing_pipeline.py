"""Tests for IndexingPipeline._process_file / index_project failure safety.

Guards the invariant that a file's previously indexed chunks are never
deleted unless the replacement chunks have been successfully embedded.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.chunker import Chunk, Chunker
from rag_mcp.config_loader import (
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    ProjectConfig,
    SourcePattern,
    StorageConfig,
)
from rag_mcp.file_reader import FileContent
from rag_mcp.indexing_pipeline import IndexingPipeline


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config() -> AppConfig:
    return AppConfig(
        embedding=EmbeddingConfig(),
        storage=StorageConfig(path="/nonexistent"),
        chunking=ChunkingConfig(chunk_size=1000, chunk_overlap=200),
        projects=[],
    )


def _make_file(rel: str, content: str, file_hash: str) -> FileContent:
    return FileContent(
        path=Path("/base") / rel,
        relative_path=rel,
        content=content,
        file_hash=file_hash,
    )


def _make_pipeline(existing_hash, encode_side_effect=None):
    """Build a pipeline with a mock store and embedder.

    `existing_hash` is what the store reports for every file.
    `encode_side_effect` is passed to embedding_gen.encode (e.g. an exception).
    """
    store = MagicMock()
    store.get_existing_hash.return_value = existing_hash
    store.get_or_create_collection.return_value = MagicMock(name="collection")

    embedding_gen = MagicMock()
    if encode_side_effect is not None:
        embedding_gen.encode.side_effect = encode_side_effect
    else:
        embedding_gen.encode.side_effect = lambda texts: [[0.0] * 4 for _ in texts]

    chunker = MagicMock(spec=Chunker)
    chunker.chunk.side_effect = lambda text: [
        Chunk(content=text, index=0, total=1)
    ]

    pipeline = IndexingPipeline(
        config=_make_config(),
        file_reader=MagicMock(),
        chunker=chunker,
        embedding_gen=embedding_gen,
        store=store,
        console=MagicMock(),
    )
    return pipeline, store, embedding_gen


SOURCE = SourcePattern(pattern="**/*.md", type="documentation", description="docs")


# ---------------------------------------------------------------------------
# _process_file
# ---------------------------------------------------------------------------


def test_embedding_failure_preserves_old_chunks():
    """If encode() raises, neither delete nor upsert may have been called."""
    pipeline, store, _ = _make_pipeline(
        existing_hash="old-hash", encode_side_effect=RuntimeError("model OOM")
    )
    collection = MagicMock()
    fc = _make_file("docs/a.md", "# changed content", "new-hash")

    with pytest.raises(RuntimeError, match="model OOM"):
        pipeline._process_file(collection, fc, SOURCE, "proj")

    store.delete_file_chunks.assert_not_called()
    store.upsert_chunks.assert_not_called()


def test_delete_happens_only_after_successful_encode():
    """On success the order must be encode -> delete_file_chunks -> upsert_chunks."""
    pipeline, store, embedding_gen = _make_pipeline(existing_hash="old-hash")

    # Record call order across the two mocks with a shared manager.
    manager = MagicMock()
    manager.attach_mock(embedding_gen.encode, "encode")
    manager.attach_mock(store.delete_file_chunks, "delete_file_chunks")
    manager.attach_mock(store.upsert_chunks, "upsert_chunks")

    collection = MagicMock()
    fc = _make_file("docs/a.md", "# changed content", "new-hash")

    created = pipeline._process_file(collection, fc, SOURCE, "proj")

    assert created == 1
    names = [c[0] for c in manager.mock_calls]
    assert names == ["encode", "delete_file_chunks", "upsert_chunks"]
    store.delete_file_chunks.assert_called_once_with(collection, "docs/a.md")
    args, _ = store.upsert_chunks.call_args
    assert args[0] is collection
    assert args[1] == "docs/a.md"
    assert args[2] == ["# changed content"]
    assert args[4]["file_hash"] == "new-hash"


def test_new_file_does_not_call_delete():
    """A file with no prior index entry is upserted without a delete."""
    pipeline, store, _ = _make_pipeline(existing_hash=None)
    fc = _make_file("docs/new.md", "brand new", "h1")

    created = pipeline._process_file(MagicMock(), fc, SOURCE, "proj")

    assert created == 1
    store.delete_file_chunks.assert_not_called()
    store.upsert_chunks.assert_called_once()


def test_unchanged_file_is_skipped():
    """Hash match -> no encode, no delete, no upsert; returns 0."""
    pipeline, store, embedding_gen = _make_pipeline(existing_hash="same")
    fc = _make_file("docs/a.md", "unchanged", "same")

    created = pipeline._process_file(MagicMock(), fc, SOURCE, "proj")

    assert created == 0
    embedding_gen.encode.assert_not_called()
    store.delete_file_chunks.assert_not_called()
    store.upsert_chunks.assert_not_called()


# ---------------------------------------------------------------------------
# index_project
# ---------------------------------------------------------------------------


def test_one_failing_file_does_not_abort_project(tmp_path, capsys):
    """A failure on one file is logged and the remaining files are still indexed."""
    (tmp_path / "bad.md").write_text("bad file")
    (tmp_path / "good.md").write_text("good file")
    
    pipeline, store, embedding_gen = _make_pipeline(existing_hash="old-hash")

    def encode(texts):
        if texts == ["bad file"]:
            raise RuntimeError("boom")
        return [[0.0] * 4 for _ in texts]

    embedding_gen.encode.side_effect = encode

    pipeline.file_reader.read.side_effect = lambda p, base: _make_file(
        p.name, p.read_text(), f"hash-{p.name}"
    )

    project = ProjectConfig(
        name="proj",
        description="",
        base_path=str(tmp_path),
        sources=[SourcePattern(pattern="*.md", type="documentation")],
    )

    total = pipeline.index_project(project)

    # Only good.md was indexed
    assert total == 1
    upserted_paths = [c.args[1] for c in store.upsert_chunks.call_args_list]
    assert upserted_paths == ["good.md"]
    # bad.md's old chunks were left alone
    deleted_paths = [c.args[1] for c in store.delete_file_chunks.call_args_list]
    assert deleted_paths == ["good.md"]
    # Failure was reported on stderr
    err = capsys.readouterr().err
    assert "failed to index bad.md" in err
    assert "RuntimeError: boom" in err
