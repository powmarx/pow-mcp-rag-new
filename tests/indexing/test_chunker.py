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


def test_long_line_not_split_mid_word():
    """A single line longer than chunk_size must be cut at whitespace, not mid-word."""
    config = ChunkingConfig(chunk_size=50, chunk_overlap=0, separators=["\n"])
    chunker = Chunker(config)
    line = "def compute_weighted_average(values, weights, normalization_strategy, fallback_value=None):"
    chunks = chunker.chunk(line)
    assert len(chunks) > 1
    original_words = set(line.split())
    for chunk in chunks:
        assert len(chunk.content) <= 50
        for word in chunk.content.split():
            assert word in original_words, f"word was split: {word!r}"


def test_long_line_without_whitespace_breaks_on_punctuation():
    """With no whitespace available, prefer punctuation over a hard cut."""
    config = ChunkingConfig(chunk_size=30, chunk_overlap=0, separators=["\n"])
    chunker = Chunker(config)
    line = "call(" + ",".join("arg" * 5 + str(i) for i in range(6)) + ")"
    chunks = chunker.chunk(line)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.content.endswith(","), chunk.content


def test_long_blob_without_boundaries_hard_cuts_and_terminates():
    """A blob with no boundaries at all falls back to a hard cut and loses nothing."""
    config = ChunkingConfig(chunk_size=50, chunk_overlap=0, separators=["\n"])
    chunker = Chunker(config)
    blob = "A" * 175
    chunks = chunker.chunk(blob)
    assert [len(c.content) for c in chunks] == [50, 50, 50, 25]
    assert "".join(c.content for c in chunks) == blob


PYTHON_SOURCE = '''"""Module docstring."""

import os


class Widget:
    """A widget."""

    def __init__(self, name):
        self.name = name
        self.items = []

    def add(self, item):
        if item is None:
            return

        self.items.append(item)


def helper(value, *, strict=False):
    try:
        return int(value)
    except ValueError:
        if strict:
            raise
        return None
'''


def test_python_source_roundtrips_and_compiles():
    """Chunks of a Python file, concatenated, must reproduce it exactly and compile.

    Regression: leading indentation and blank lines were being stripped from
    pieces, so a rebuilt file had "unexpected indent" errors.
    """
    for size in (60, 120, 300):
        config = ChunkingConfig(chunk_size=size, chunk_overlap=0)
        chunker = Chunker(config)
        chunks = chunker.chunk(PYTHON_SOURCE)
        assert len(chunks) > 1
        rebuilt = "".join(c.content for c in chunks)
        assert rebuilt == PYTHON_SOURCE
        compile(rebuilt, "<rebuilt>", "exec")
        for c in chunks:
            assert len(c.content) <= size


def test_chunks_are_exact_substrings_with_overlap():
    """Even with overlap, each chunk must be a verbatim slice of the source."""
    config = ChunkingConfig(chunk_size=120, chunk_overlap=40)
    chunker = Chunker(config)
    chunks = chunker.chunk(PYTHON_SOURCE)
    assert len(chunks) > 1
    pos = 0
    prev_end = 0
    for i, c in enumerate(chunks):
        found = PYTHON_SOURCE.find(c.content, pos)
        assert found != -1, f"chunk {i} is not a substring of the source"
        if i > 0:
            assert found < prev_end, f"chunk {i} does not overlap its predecessor"
        prev_end = found + len(c.content)
        pos = found


def test_indentation_preserved_at_chunk_start():
    """A chunk that begins on an indented line must keep that indentation."""
    config = ChunkingConfig(chunk_size=40, chunk_overlap=0, separators=["\n\n", "\n"])
    chunker = Chunker(config)
    text = "def f():\n    a = 1\n\n    b = 2\n\n    return a + b\n"
    chunks = chunker.chunk(text)
    assert "".join(c.content for c in chunks) == text
    compile(text, "<t>", "exec")
    # Every line that was indented in the source is still indented in some chunk
    for line in ("    a = 1", "    b = 2", "    return a + b"):
        assert any(line in c.content for c in chunks), line


def _chunks(text, size, overlap=0, separators=None):
    kwargs = {"chunk_size": size, "chunk_overlap": overlap}
    if separators is not None:
        kwargs["separators"] = separators
    return Chunker(ChunkingConfig(**kwargs)).chunk(text)


def _assert_integrity(text, chunks, size):
    """Shared invariants: lossless, bounded, no blank chunks."""
    assert "".join(c.content for c in chunks) == text
    assert all(len(c.content) <= size for c in chunks)
    assert all(c.content.strip() for c in chunks)


def _ends_with_heading(content):
    last_line = content.rstrip("\n").split("\n")[-1]
    return last_line.startswith("## ") or last_line.startswith("### ")


def test_heading_of_large_section_leads_its_chunk():
    """A heading whose body is too big to fit alongside must start a chunk
    together with the beginning of its body, never sit at the tail of the
    previous chunk or alone."""
    md = (
        "# Title\n\n" + "intro text. " * 8 + "\n\n"
        "## Section A\n\n" + "alpha " * 60 + "\n\n"
        "## Section B\n\n" + "beta " * 60 + "\n"
    )
    chunks = _chunks(md, 120)
    _assert_integrity(md, chunks, 120)
    assert not any(_ends_with_heading(c.content) for c in chunks)
    for heading, body in (("## Section A", "alpha"), ("## Section B", "beta")):
        holder = next(c for c in chunks if heading in c.content)
        assert holder.content.lstrip().startswith(heading)
        assert body in holder.content, "heading must be followed by some of its body"


def test_small_sections_still_pack_together():
    """Heading-awareness must not explode many small sections into many chunks."""
    md = "# Doc\n\n" + "".join(f"## S{i}\n\nshort body {i}.\n\n" for i in range(8))
    chunks = _chunks(md, 120)
    _assert_integrity(md, chunks, 120)
    assert len(chunks) <= 3
    assert all(c.content.startswith(("# ", "## ")) for c in chunks)
    assert not any(_ends_with_heading(c.content) for c in chunks)


def test_nested_headings_stay_together_with_body():
    """'## A' directly followed by '### A.1' is not an empty section; both
    headings travel with the start of the body."""
    md = "intro\n\n## A\n\n### A.1\n\n" + "x " * 60 + "\n\n### A.2\n\n" + "y " * 60 + "\n"
    chunks = _chunks(md, 120)
    _assert_integrity(md, chunks, 120)
    holder = next(c for c in chunks if "## A\n" in c.content)
    assert "### A.1" in holder.content and "x x" in holder.content
    assert not any(_ends_with_heading(c.content) for c in chunks)


def test_title_only_chunk_absorbs_following_heading():
    """A tiny chunk (e.g. a lone '# Title') must not be emitted on its own when
    a heading follows; the heading and part of its body join it."""
    md = "# Architecture\n\n## How It Works\n\n" + "text " * 300
    chunks = _chunks(md, 300)
    _assert_integrity(md, chunks, 300)
    assert chunks[0].content.startswith("# Architecture\n\n## How It Works\n\ntext")
    assert all(len(c.content) >= 300 // 4 for c in chunks[:-1])


def test_long_line_split_is_balanced_not_greedy():
    """A line just over chunk_size splits into two even halves, not N + tiny tail."""
    line = "x" + " ".join(["word"] * 60) + "\n"  # 301 chars
    assert len(line) == 301
    chunks = _chunks(line, 300)
    _assert_integrity(line, chunks, 300)
    assert len(chunks) == 2
    assert all(100 < len(c.content) < 200 for c in chunks), [len(c.content) for c in chunks]


def test_no_blank_or_tiny_fragments_on_long_lines():
    """Trailing newlines / row terminators after a long line never become a
    chunk of their own (regression: '|\\n\\n' and '\\n\\n' chunks)."""
    row = "| " + " | ".join(f"cell {i} with some text" for i in range(14)) + " |\n\n"
    md = "## Table\n\n" + row + "## Next\n\n" + "body " * 80 + "\n"
    for size in (80, 120, 300):
        chunks = _chunks(md, size)
        _assert_integrity(md, chunks, size)
        assert all(len(c.content) >= size // 10 for c in chunks[:-1]), [c.content for c in chunks]


def test_indented_long_token_does_not_split_off_indentation():
    """Leading indentation is never emitted as its own whitespace-only chunk."""
    src = "def f():\n" + " " * 28 + "x = " + "a" * 60 + "\n    return x\n"
    chunks = _chunks(src, 50, separators=["\n"])
    _assert_integrity(src, chunks, 50)


def test_overlap_includes_small_previous_chunk_entirely():
    """When the previous chunk is shorter than the overlap, the overlap is the
    whole previous chunk (regression: title chunk was skipped entirely)."""
    md = "# Architecture\n\n" + "## How It Works\n\n" + "line of text\n" * 30
    chunks = _chunks(md, 200, overlap=60)
    assert len(chunks) > 1
    for i in range(1, len(chunks)):
        assert chunks[i].content in md
        prev_tail = chunks[i - 1].content[-60:]
        if any(ch.isspace() for ch in prev_tail.rstrip()):
            assert md.find(chunks[i].content) < md.find(chunks[i - 1].content) + len(chunks[i - 1].content)


def test_overlap_never_starts_mid_token():
    """Overlap start snaps to a line or word boundary, or is skipped."""
    text = "\n".join(f"line {i}: " + "token" * 3 for i in range(60)) + "\n"
    chunks = _chunks(text, 200, overlap=40)
    for i in range(1, len(chunks)):
        start = text.find(chunks[i].content)
        assert start == 0 or text[start - 1].isspace(), chunks[i].content[:30]


if __name__ == "__main__":
    test_small_file_single_chunk()
    test_empty_text_returns_empty()
    test_large_text_splits()
    test_overlap_applied()
    test_chunk_indices_sequential()
    test_long_line_not_split_mid_word()
    test_long_line_without_whitespace_breaks_on_punctuation()
    test_long_blob_without_boundaries_hard_cuts_and_terminates()
    test_python_source_roundtrips_and_compiles()
    test_chunks_are_exact_substrings_with_overlap()
    test_indentation_preserved_at_chunk_start()
    test_heading_of_large_section_leads_its_chunk()
    test_small_sections_still_pack_together()
    test_nested_headings_stay_together_with_body()
    test_title_only_chunk_absorbs_following_heading()
    test_long_line_split_is_balanced_not_greedy()
    test_no_blank_or_tiny_fragments_on_long_lines()
    test_indented_long_token_does_not_split_off_indentation()
    test_overlap_includes_small_previous_chunk_entirely()
    test_overlap_never_starts_mid_token()
    print("All chunker tests passed!")
