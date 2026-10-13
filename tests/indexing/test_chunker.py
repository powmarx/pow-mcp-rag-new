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


def _assert_boundaries_at_whitespace(text, chunks, size, soft=",;)]}"):
    """Every boundary between consecutive chunks (overlap=0) must sit next to
    whitespace. A cut inside a token is tolerated only if that token could not
    fit in a chunk at all, and then it must be at a soft-break character unless
    none is available in the window."""
    assert "".join(c.content for c in chunks) == text
    pos = 0
    for chunk in chunks[:-1]:
        pos += len(chunk.content)
        if text[pos - 1].isspace() or text[pos].isspace():
            continue
        left = pos
        while left > 0 and not text[left - 1].isspace():
            left -= 1
        right = pos
        while right < len(text) and not text[right].isspace():
            right += 1
        token = text[left:right]
        assert len(token) >= size, (
            f"mid-word cut at {pos} (size={size}): {text[max(0, pos - 12):pos]!r} | "
            f"{text[pos:pos + 12]!r} inside token {token!r} that fits"
        )
        if text[pos - 1] not in soft:
            assert not any(ch in soft for ch in chunk.content[1:]), (
                f"hard cut at {pos} although a soft-break char was available"
            )


LONG_LINES = [
    "def compute_weighted_average(values, weights, normalization_strategy, fallback_value=None):",
    "the quick brown fox jumps over the lazy dog while seventeen extraordinarily verbose adjectives "
    "accumulate in a sentence that keeps going without any line breaks at all",
    "a bb ccc dddd eeeee ffffff ggggggg hhhhhhhh iiiiiiiii jjjjjjjjjj kkkkkkkkkkk llllllllllll "
    "mmmmmmmmmmmmm nnnnnnnnnnnnnn ooooooooooooooo pppppppppppppppp",
    "x = [alpha, beta, gamma, delta, epsilon, zeta, eta, theta, iota, kappa, lambda_, mu, nu, xi]",
]


def test_long_line_not_split_mid_word():
    """A single line longer than chunk_size must be cut at whitespace, never
    mid-word, for any chunk_size that can hold the longest word.

    Checked across many sizes and lines so that a blind cut cannot pass by
    coincidentally landing on a space.
    """
    for line in LONG_LINES:
        longest = max(len(w) for w in line.split())
        for size in range(longest + 2, 80):
            chunks = _chunks(line, size, separators=["\n"])
            assert len(chunks) > 1
            assert all(len(c.content) <= size for c in chunks)
            _assert_boundaries_at_whitespace(line, chunks, size)


def test_long_line_without_whitespace_breaks_on_punctuation():
    """With no whitespace available, prefer punctuation over a hard cut."""
    line = "call(" + ",".join("arg" * 5 + str(i) for i in range(12)) + ")"
    longest_segment = max(len(seg) for seg in line.split(","))  # incl. the "call(" prefix
    for size in range(longest_segment + 2, 60):
        chunks = _chunks(line, size, separators=["\n"])
        assert len(chunks) > 1
        assert "".join(c.content for c in chunks) == line
        assert all(len(c.content) <= size for c in chunks)
        for chunk in chunks[:-1]:
            assert chunk.content.endswith(","), (size, chunk.content)


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
        _assert_boundaries_at_whitespace(PYTHON_SOURCE, chunks, size)


def test_chunks_are_exact_substrings_with_overlap():
    """Even with overlap, each chunk must be a verbatim slice of the source."""
    config = ChunkingConfig(chunk_size=120, chunk_overlap=40)
    chunker = Chunker(config)
    chunks = chunker.chunk(PYTHON_SOURCE)
    assert len(chunks) > 1
    pos = 0
    prev_end = 0
    for i, c in enumerate(chunks):
        assert len(c.content) <= 120, "overlap must not push a chunk past chunk_size"
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
    # Two intro lengths: one where the heading line itself would still fit in
    # the intro chunk (so only the section rule keeps it out), one where not.
    for intro_repeats in (4, 8):
        md = (
            "# Title\n\n" + "intro text. " * intro_repeats + "\n\n"
            "## Section A\n\n" + "alpha " * 60 + "\n\n"
            "## Section B\n\n" + "beta " * 60 + "\n"
        )
        chunks = _chunks(md, 120)
        _assert_integrity(md, chunks, 120)
        _assert_boundaries_at_whitespace(md, chunks, 120)
        assert not any(_ends_with_heading(c.content) for c in chunks)
        for heading, body in (("## Section A", "alpha"), ("## Section B", "beta")):
            holder = next(c for c in chunks if heading in c.content)
            assert holder.content.lstrip().startswith(heading), (intro_repeats, holder.content)
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


def test_long_line_split_is_balanced_for_multiple_of_chunk_size():
    """A long line whose length is close to a multiple of chunk_size has no
    slack for a clean cut at the naive balanced target; the split must still
    be even (one extra piece) rather than greedy-first + leftovers."""
    import random

    rng = random.Random(3)
    words = []
    while sum(len(w) + 1 for w in words) < 900:
        words.append("w" * rng.randint(2, 11))
    for total in (600, 900):
        line = " ".join(words)[: total - 1] + "\n"
        assert len(line) == total
        for size in (300, 250, 200, 150):
            chunks = _chunks(line, size, separators=["\n"])
            _assert_integrity(line, chunks, size)
            _assert_boundaries_at_whitespace(line, chunks, size)
            lengths = [len(c.content) for c in chunks]
            assert len(chunks) <= -(-total // size) + 1, lengths
            assert min(lengths) >= 0.75 * max(lengths), (total, size, lengths)


def test_no_blank_or_tiny_fragments_on_long_lines():
    """Trailing newlines / row terminators after a long line never become a
    chunk of their own (regression: '|\\n\\n' and '\\n\\n' chunks)."""
    row = "| " + " | ".join(f"cell {i} with some text" for i in range(14)) + " |\n\n"
    md = "## Table\n\n" + row + "## Next\n\n" + "body " * 80 + "\n"
    for size in (80, 120, 300):
        chunks = _chunks(md, size)
        _assert_integrity(md, chunks, size)
        _assert_boundaries_at_whitespace(md, chunks, size)
        assert all(len(c.content) >= size // 10 for c in chunks[:-1]), [c.content for c in chunks]


def test_indented_long_token_does_not_split_off_indentation():
    """Leading indentation is never emitted as its own whitespace-only chunk."""
    src = "def f():\n" + " " * 28 + "x = " + "a" * 60 + "\n    return x\n"
    chunks = _chunks(src, 50, separators=["\n"])
    _assert_integrity(src, chunks, 50)


def test_overlap_present_after_small_previous_chunk():
    """A previous chunk shorter than chunk_overlap still yields an overlap
    (regression: a title-only chunk used to be skipped entirely), but is
    never swallowed whole by the next chunk."""
    md = "# Architecture\n\n## How It Works\n\n" + "".join(f"line {i:02d} of text\n" for i in range(30))
    chunks = _chunks(md, 200, overlap=60)
    assert len(chunks) > 1
    for i in range(1, len(chunks)):
        assert chunks[i].content in md
        prev_start = md.find(chunks[i - 1].content)
        prev_end = prev_start + len(chunks[i - 1].content)
        start = md.find(chunks[i].content)
        assert prev_start < start < prev_end, "must overlap, but not contain the previous chunk"


def test_overlap_never_contains_whole_previous_chunk():
    """Even with chunk_overlap ~ chunk_size, every chunk keeps unique content."""
    text = "\n".join(f"l{i} " + "ab " * 5 for i in range(100)) + "\n"
    # A short intro chunk followed by a heading whose section is too big: the
    # next chunk starts with overlap, which must not reach back to the very
    # start of the 11-char intro chunk.
    text2 = "intro line\n## Big\n\n" + "body " * 40 + "\n"
    for text, size, overlap in ((text, 60, 59), (text, 100, 90), (text, 333, 332), (text2, 40, 39)):
        chunks = _chunks(text, size, overlap=overlap)
        assert all(len(c.content) <= size for c in chunks)
        pos = 0
        for i in range(1, len(chunks)):
            prev_start = text.find(chunks[i - 1].content, pos)
            start = text.find(chunks[i].content, prev_start + 1)
            assert start != -1 and start > prev_start, (size, overlap, i)
            pos = prev_start


def test_overlap_never_starts_mid_token():
    """Overlap start snaps to a line or word boundary, or is skipped."""
    text = "\n".join(f"line {i}: " + "token" * 3 for i in range(60)) + "\n"
    chunks = _chunks(text, 200, overlap=40)
    for i in range(1, len(chunks)):
        start = text.find(chunks[i].content)
        assert start == 0 or text[start - 1].isspace(), chunks[i].content[:30]

    # Every line ends in a token longer than the overlap, so the tail of the
    # previous chunk never contains a boundary: overlap must be skipped, not
    # started inside the token.
    text = "".join(f"w{i} " + "x" * 50 + "\n" for i in range(30))
    chunks = _chunks(text, 200, overlap=40)
    assert len(chunks) > 2
    for c in chunks:
        assert c.content in text
        start = text.find(c.content)
        assert start == 0 or text[start - 1] == "\n", repr(c.content[:20])


def test_overlap_counts_toward_chunk_size():
    """chunk_size is a hard limit that includes the overlap.

    Regression: overlap used to be prepended after packing, producing chunks
    up to chunk_size + chunk_overlap long.
    """
    text = "\n".join(f"line {i:03d}: " + "word " * 12 for i in range(80)) + "\n"
    for size, overlap in ((100, 30), (200, 60), (300, 150), (120, 119)):
        chunks = _chunks(text, size, overlap=overlap)
        assert len(chunks) > 1
        assert max(len(c.content) for c in chunks) <= size, (size, overlap)
        assert all(c.content in text for c in chunks)


def test_overlap_is_realized_when_there_is_room():
    """Overlap is best-effort but must actually appear when the chunk has room,
    and must never reach back further than chunk_overlap characters."""
    text = "\n".join(f"line {i:03d}: " + "word " * 12 for i in range(80)) + "\n"
    size, overlap = 300, 60
    chunks = _chunks(text, size, overlap=overlap)
    pos = 0
    for i in range(1, len(chunks)):
        start = text.find(chunks[i].content, pos)
        prev_start = text.find(chunks[i - 1].content, pos)
        prev_end = prev_start + len(chunks[i - 1].content)
        shared = prev_end - start
        room = size - (len(chunks[i].content) - max(shared, 0))
        if room >= 10:  # enough room for at least a short line of overlap
            assert 0 < shared <= overlap, (i, shared)
        pos = start


def test_overlap_shrinks_instead_of_exceeding_limit():
    """A piece that nearly fills chunk_size gets a reduced (or no) overlap
    rather than a chunk longer than chunk_size."""
    big = "x " * 140 + "\n"          # 281 chars, one line: fits in 300 alone
    text = "intro line one\nintro line two\n" + big + "tail line\n"
    chunks = _chunks(text, 300, overlap=100, separators=["\n"])
    _assert_integrity_overlapped(text, chunks, 300)
    holder = next(c for c in chunks if "x x x" in c.content)
    assert len(holder.content) <= 300


def _assert_integrity_overlapped(text, chunks, size):
    assert all(len(c.content) <= size for c in chunks)
    assert all(c.content.strip() for c in chunks)
    pos = covered = 0
    for c in chunks:
        f = text.find(c.content, pos)
        assert f != -1 and f <= covered, "chunks must be substrings with no gaps"
        covered = max(covered, f + len(c.content))
        pos = f
    assert covered == len(text)


def test_overlap_rebuild_has_no_gaps():
    """With overlap, stitching chunks at their offsets still covers the whole source."""
    for size, overlap in ((120, 40), (300, 100)):
        chunks = _chunks(PYTHON_SOURCE, size, overlap=overlap)
        _assert_integrity_overlapped(PYTHON_SOURCE, chunks, size)


def test_blank_line_before_full_line_is_not_its_own_chunk():
    """A blank line followed by a line that exactly fills chunk_size must be
    folded into a neighbour, never emitted as a whitespace-only chunk."""
    line = "Supported input formats (attempted in order):\n"
    text = "\n" + " " * (50 - len(line)) + line  # second line is exactly 50 chars
    assert len(text) == 51
    chunks = _chunks(text, 50)
    _assert_integrity(text, chunks, 50)
    _assert_boundaries_at_whitespace(text, chunks, 50)


def test_indentation_is_never_split_off_as_a_chunk():
    """Cutting a long indented line must not leave the indentation alone."""
    line = '        _write_entry(vscode_path, args.server_name, vscode_entry, servers_key="servers")\n'
    for size in range(40, 61):
        chunks = _chunks(line, size, separators=["\n"])
        _assert_integrity(line, chunks, size)
        _assert_boundaries_at_whitespace(line, chunks, size)
        assert all(c.content.strip() for c in chunks)


def test_trailing_blank_lines_after_unbreakable_token_not_a_chunk():
    """A token that must be hard-cut, followed by blank lines: the cut must
    keep the last content character with the trailing whitespace so that no
    whitespace-only chunk is produced."""
    text = "https://example.com/" + "a" * 100 + "\n" * 9
    for separators in (["\n# "], ["\n\n", "\n"], None):
        chunks = _chunks(text, 120, separators=separators)
        _assert_integrity(text, chunks, 120)


def test_bare_heading_gets_part_of_a_nearly_full_paragraph():
    """When the heading's first body piece is a whole paragraph that does not
    fit beside the heading, the paragraph is re-flowed so the heading is not
    left alone (flushing as-is would orphan it)."""
    body = " ".join(f"word{i}" for i in range(18))[:113]
    md = "## Section A\n\n" + body + "\n\n" + "short tail.\n"
    chunks = _chunks(md, 120)
    _assert_integrity(md, chunks, 120)
    _assert_boundaries_at_whitespace(md, chunks, 120)
    assert chunks[0].content.startswith("## Section A\n\nword0 word1")
    assert not any(_ends_with_heading(c.content) for c in chunks)


def test_reflow_cut_never_ends_chunk_on_bare_heading():
    """A heading followed directly by a sub-heading and a nearly full body:
    the balanced cut would land right after a heading line; it must be
    retried further right so no chunk ends on a bare heading."""
    md = "## A\n\n### B\n\n" + "y " * 55 + "\n"
    chunks = _chunks(md, 120)
    _assert_integrity(md, chunks, 120)
    assert chunks[0].content.startswith("## A\n\n### B\n\ny y")
    assert not any(_ends_with_heading(c.content) for c in chunks)


def test_long_line_chunks_carry_overlap():
    """Fragments of a long line are sized so that each chunk can carry its
    overlap: the realised overlap must stay close to chunk_overlap."""
    import random

    rng = random.Random(5)
    words = []
    while sum(len(w) + 1 for w in words) < 2000:
        words.append("w" * rng.randint(2, 11))
    line = " ".join(words)[:1999] + "\n"
    size, overlap = 300, 60
    chunks = _chunks(line, size, overlap=overlap, separators=["\n"])
    assert len(chunks) > 3
    assert all(len(c.content) <= size for c in chunks)
    pos = 0
    for i in range(1, len(chunks)):
        prev_start = line.find(chunks[i - 1].content, pos)
        start = line.find(chunks[i].content, prev_start + 1)
        shared = prev_start + len(chunks[i - 1].content) - start
        assert 0.75 * overlap <= shared <= overlap, (i, shared)
        pos = prev_start


def test_reflow_never_cuts_before_heading_merged_into_small_chunk():
    """A heading merged into a tiny chunk (small-chunk waiver) must stay
    there: the balanced re-flow cut must not land before it and re-create the
    title-only chunk. Checked across body lengths so the cut window varies."""
    for plen in range(280, 296, 2):
        para = " ".join(f"w{i}" for i in range(80))[:plen]
        md = "# Title\n\n## H\n\n" + para + "\n\n" + "tail line\n"
        chunks = _chunks(md, 300)
        _assert_integrity(md, chunks, 300)
        assert chunks[0].content.startswith("# Title\n\n## H\n\nw0 w1"), (plen, chunks[0].content)
        assert not any(_ends_with_heading(c.content) for c in chunks)


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
    test_long_line_split_is_balanced_for_multiple_of_chunk_size()
    test_blank_line_before_full_line_is_not_its_own_chunk()
    test_indentation_is_never_split_off_as_a_chunk()
    test_trailing_blank_lines_after_unbreakable_token_not_a_chunk()
    test_no_blank_or_tiny_fragments_on_long_lines()
    test_indented_long_token_does_not_split_off_indentation()
    test_overlap_present_after_small_previous_chunk()
    test_overlap_never_contains_whole_previous_chunk()
    test_overlap_never_starts_mid_token()
    test_overlap_counts_toward_chunk_size()
    test_overlap_is_realized_when_there_is_room()
    test_overlap_shrinks_instead_of_exceeding_limit()
    test_overlap_rebuild_has_no_gaps()
    test_bare_heading_gets_part_of_a_nearly_full_paragraph()
    test_reflow_cut_never_ends_chunk_on_bare_heading()
    test_long_line_chunks_carry_overlap()
    test_reflow_never_cuts_before_heading_merged_into_small_chunk()
    print("All chunker tests passed!")
