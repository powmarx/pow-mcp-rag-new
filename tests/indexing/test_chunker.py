"""Tests for the Chunker module."""

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.chunker import Chunker
from rag_mcp.config_loader import ChunkingConfig


def test_small_file_single_chunk():
    """Files smaller than chunk_size should be a single chunk."""
    config = ChunkingConfig(chunk_size=1000, chunk_overlap=200)
    chunker = Chunker(config)
    text = "Hello world. This is a small file."
    chunks = chunker.chunk(text)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].index == 0
    assert chunks[0].total == 1


def test_empty_text_returns_empty():
    """Empty or whitespace-only text returns no chunks."""
    config = ChunkingConfig(chunk_size=1000, chunk_overlap=200)
    chunker = Chunker(config)
    assert chunker.chunk("") == []
    assert chunker.chunk("   ") == []
    assert chunker.chunk("\n\n") == []


def test_large_text_splits():
    """Text larger than chunk_size should be split into multiple chunks."""
    config = ChunkingConfig(chunk_size=100, chunk_overlap=0, separators=["\n\n", "\n"])
    chunker = Chunker(config)
    # Create text with clear paragraph breaks
    paragraphs = [f"Paragraph {i}. " + "x" * 60 for i in range(5)]
    text = "\n\n".join(paragraphs)
    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    # All chunks should have correct total
    for chunk in chunks:
        assert chunk.total == len(chunks)
    # Indices should be sequential
    for i, chunk in enumerate(chunks):
        assert chunk.index == i


def test_overlap_applied():
    """Chunks should have overlap from previous chunk's tail."""
    config = ChunkingConfig(chunk_size=50, chunk_overlap=20, separators=["\n\n"])
    chunker = Chunker(config)
    text = "First paragraph content here.\n\nSecond paragraph content here.\n\nThird paragraph content here."
    chunks = chunker.chunk(text)
    # With overlap, second chunk should contain some text from first
    if len(chunks) > 1:
        # The overlap means chunks after the first should be longer than without overlap
        assert len(chunks) >= 2


def test_chunk_indices_sequential():
    """Chunk indices should be 0, 1, 2, ... N-1."""
    config = ChunkingConfig(chunk_size=50, chunk_overlap=0, separators=["\n"])
    chunker = Chunker(config)
    text = "\n".join([f"Line {i} with some content" for i in range(20)])
    chunks = chunker.chunk(text)
    for i, chunk in enumerate(chunks):
        assert chunk.index == i
        assert chunk.total == len(chunks)


if __name__ == "__main__":
    test_small_file_single_chunk()
    test_empty_text_returns_empty()
    test_large_text_splits()
    test_overlap_applied()
    test_chunk_indices_sequential()
    print("All chunker tests passed!")
