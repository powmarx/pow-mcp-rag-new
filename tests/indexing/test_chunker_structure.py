"""Tests for the chunker's document-structure awareness.

Covers:
* fenced code blocks are never split when they fit, and nothing inside a
  fence is treated as a heading;
* underlined (reStructuredText / plain-text) titles are recognised as
  sections and never separated from their adornment;
* section breadcrumbs are attached to every chunk (metadata only);
* the indexing pipeline stores the breadcrumb only for .md/.txt/.rst.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.chunker import (
    BREADCRUMB_EXTENSIONS,
    Chunk,
    Chunker,
    breadcrumbs_enabled,
    chunk_metadatas,
)
from rag_mcp.config_loader import ChunkingConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunker(size=300, overlap=50) -> Chunker:
    return Chunker(ChunkingConfig(chunk_size=size, chunk_overlap=overlap))


def _assert_lossless(chunker: Chunker, text: str) -> None:
    """With overlap=0, chunks partition the text exactly; always bounded."""
    no_overlap = Chunker(ChunkingConfig(chunk_size=chunker.chunk_size, chunk_overlap=0))
    chunks = no_overlap.chunk(text)
    assert "".join(c.content for c in chunks) == text
    for c in chunker.chunk(text):
        assert len(c.content) <= chunker.chunk_size
        assert c.content in text


def _code_block(n_funcs: int, fence: str = "```python") -> str:
    body = "\n".join(f"def func_{i}():\n    return {i}\n" for i in range(n_funcs))
    return f"{fence}\n{body}{fence[:3]}\n"


# ---------------------------------------------------------------------------
# Code fences
# ---------------------------------------------------------------------------


def test_fitting_fence_is_never_split():
    """Regression: a fenced block that fits in chunk_size stays in one chunk."""
    chunker = _chunker(300, 50)
    text = (
        "# Title\n\n" + "Intro paragraph here. " * 5 + "\n\n## Usage\n\n"
        + _code_block(8)
        + "\n## Next section\n\n" + "More prose follows here. " * 10 + "\n"
    )
    fence_len = text.index("```\n\n## Next") + 4 - text.index("```python")
    assert fence_len <= 300, "test setup: fence must fit in a chunk"

    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    for c in chunks:
        assert c.content.count("```") % 2 == 0, f"unbalanced fence in chunk {c.index}"
    _assert_lossless(chunker, text)


def test_tilde_fence_is_protected_too():
    chunker = _chunker(300, 50)
    text = "Intro.\n\n" + "Words here. " * 20 + "\n\n" + _code_block(8, "~~~") + "\n" + "Tail text. " * 25
    for c in chunker.chunk(text):
        assert c.content.count("~~~") % 2 == 0


def test_heading_marker_inside_fence_is_not_a_section():
    """'## ' inside a fence must not act as a heading / cut point."""
    chunker = _chunker(200, 0)
    fence = "```bash\n## not a heading, just a comment\necho hi\n## another comment\necho bye\n```\n"
    text = "Intro words. " * 12 + "\n\n" + fence + "\nAfter. " * 20
    chunks = chunker.chunk(text)
    fence_chunks = [c for c in chunks if "not a heading" in c.content]
    assert len(fence_chunks) == 1
    assert fence_chunks[0].content.count("```") == 2
    # A fence is never a breadcrumb source
    assert all("not a heading" not in c.breadcrumb for c in chunks)
    _assert_lossless(chunker, text)


def test_heading_inside_fence_not_in_breadcrumb():
    chunker = _chunker(1000, 0)
    text = "# Real\n\n```\n# Fake heading in code\n```\n\nBody.\n"
    [c] = chunker.chunk(text)
    assert c.breadcrumb == "Real"


def test_oversized_fence_still_bounded_and_lossless():
    chunker = _chunker(200, 40)
    text = "# Doc\n\n" + _code_block(40) + "\nAfter.\n"
    chunks = chunker.chunk(text)
    assert len(chunks) > 2
    for c in chunks:
        assert len(c.content) <= 200
    _assert_lossless(chunker, text)
    # Even split across chunks, fence content is never used as a heading
    assert all(c.breadcrumb == "Doc" for c in chunks)


def test_unclosed_fence_runs_to_end():
    chunker = _chunker(1000, 0)
    text = "# Doc\n\n```\n## looks like heading\nno closing fence\n"
    [c] = chunker.chunk(text)
    assert c.breadcrumb == "Doc"


def test_overlap_never_starts_inside_a_fence():
    """The overlap tail carried into the next chunk must not open a fence."""
    chunker = _chunker(300, 100)
    text = "Intro text. " * 10 + "\n\n" + _code_block(8) + "\n" + "Epilogue words. " * 30
    for c in chunker.chunk(text):
        assert c.content.count("```") % 2 == 0


# ---------------------------------------------------------------------------
# Underlined titles (RST / plain text)
# ---------------------------------------------------------------------------


RST_DOC = (
    "Project\n=======\n\nIntro paragraph.\n\n"
    "Install\n-------\n\n" + "pip install it. " * 20 + "\n\n"
    "Usage\n-----\n\n" + "Run it. " * 30 + "\n\n"
    "Advanced\n~~~~~~~~\n\n" + "Deep stuff. " * 20 + "\n"
)


def test_rst_underline_headings_are_cut_points():
    chunker = _chunker(300, 0)
    chunks = chunker.chunk(RST_DOC)
    starts = [c.content for c in chunks]
    # Sections whose predecessor chunk is already substantial start a new chunk
    assert any(s.startswith("Usage\n-----\n") for s in starts)
    assert any(s.startswith("Advanced\n~~~~~~~~\n") for s in starts)
    # A heading is never orphaned: wherever a title block appears, its body follows
    for c in chunks:
        for block in ("Install\n-------\n\n", "Usage\n-----\n\n", "Advanced\n~~~~~~~~\n\n"):
            if block in c.content:
                assert not c.content.rstrip().endswith(block.rstrip())
    _assert_lossless(chunker, RST_DOC)


def test_rst_title_never_separated_from_underline():
    chunker = _chunker(120, 20)
    for c in chunker.chunk(RST_DOC):
        for title, under in (("Install", "-------"), ("Usage", "-----"), ("Advanced", "~~~~~~~~")):
            if f"{title}\n" in c.content:
                assert f"{title}\n{under}" in c.content
            if c.content.rstrip().endswith(under):
                assert f"{title}\n{under}" in c.content


def test_rst_breadcrumbs_nesting():
    """The breadcrumb is the section in which the chunk's own content starts."""
    chunker = _chunker(300, 50)
    chunks = chunker.chunk(RST_DOC)
    assert chunks[0].content.startswith("Project\n=======")
    assert chunks[0].breadcrumb == "Project"
    # Continuation of the Install body (starts mid-section, after overlap)
    install_cont = next(c for c in chunks[1:] if "pip install" in c.content and "Usage" not in c.content)
    assert install_cont.breadcrumb == "Project > Install"
    usage = next(c for c in chunks if c.content.lstrip().startswith("Usage\n-----") or "Usage\n-----" in c.content)
    assert usage.breadcrumb == "Project > Usage"
    # "~" first appears after "-", so it is one level deeper (docutils rule)
    advanced = next(c for c in chunks if "Advanced\n~~~~~~~~" in c.content)
    assert advanced.breadcrumb == "Project > Usage > Advanced"


def test_rst_overline_title():
    chunker = _chunker(1000, 0)
    text = "=======\nProject\n=======\n\nBody.\n\nSub\n---\n\nMore.\n"
    [c] = chunker.chunk(text)
    assert c.breadcrumb == "Project"
    chunker = _chunker(40, 0)
    chunks = chunker.chunk(text)
    assert chunks[0].content.startswith("=======\nProject\n=======")
    assert any(c.breadcrumb == "Project > Sub" for c in chunks)


def test_txt_underlined_titles_give_breadcrumbs():
    chunker = _chunker(200, 0)
    text = (
        "RELEASE NOTES\n=============\n\n"
        "Version 2.0\n-----------\n\n" + "Added things. " * 12 + "\n\n"
        "Version 1.0\n-----------\n\n" + "Initial release. " * 12 + "\n"
    )
    chunks = chunker.chunk(text)
    crumbs = {c.breadcrumb for c in chunks}
    assert "RELEASE NOTES > Version 2.0" in crumbs
    assert "RELEASE NOTES > Version 1.0" in crumbs


def test_txt_without_headings_has_empty_breadcrumb_and_paragraph_chunks():
    chunker = _chunker(200, 0)
    paragraphs = ["Paragraph %d. " % i * 8 for i in range(6)]
    text = "\n\n".join(paragraphs) + "\n"
    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    assert all(c.breadcrumb == "" for c in chunks)
    # Paragraph boundaries are respected: no chunk ends mid-paragraph
    for c in chunks[:-1]:
        assert c.content.endswith("\n\n")


@pytest.mark.parametrize(
    "text",
    [
        "key: value\n---\nnext: doc\n",  # YAML separator shorter than line
        "| a | b |\n|---|---|\n| 1 | 2 |\n",  # markdown table
        "Some text\n\n***\n\nMore text\n",  # thematic break after blank line
        "    indented\n    --------\n",  # indented: not a title
        "===\n",  # adornment alone
    ],
)
def test_underline_grammar_rejects_false_positives(text):
    chunker = _chunker(1000, 0)
    [c] = chunker.chunk(text)
    assert c.breadcrumb == ""


def test_underline_must_be_at_least_title_length():
    chunker = _chunker(1000, 0)
    [c] = chunker.chunk("A long title line\n---\n\nBody.\n")
    assert c.breadcrumb == ""
    [c] = chunker.chunk("A long title line\n-----------------\n\nBody.\n")
    assert c.breadcrumb == "A long title line"


# ---------------------------------------------------------------------------
# Markdown breadcrumbs
# ---------------------------------------------------------------------------


MD_DOC = (
    "# Guide\n\nPreamble text.\n\n"
    "## Install\n\n" + "Step one. " * 20 + "\n\n"
    "### Quick Start\n\n" + "Run this. " * 20 + "\n\n"
    "## Usage\n\n" + "Use it like so. " * 20 + "\n"
)


def test_md_breadcrumb_path():
    chunker = _chunker(250, 40)
    chunks = chunker.chunk(MD_DOC)
    assert chunks[0].content.startswith("# Guide")
    assert chunks[0].breadcrumb == "Guide"
    quick = next(c for c in chunks if "### Quick Start" in c.content)
    assert quick.breadcrumb in ("Guide > Install", "Guide > Install > Quick Start")
    # A chunk starting inside the Quick Start body is labelled with the full path
    quick_cont = next(
        c for c in chunks if "Run this" in c.content and "### Quick Start" not in c.content
        and "## Usage" not in c.content
    ) if any("Run this" in c.content and "### Quick Start" not in c.content
             and "## Usage" not in c.content for c in chunks) else None
    if quick_cont is not None:
        assert quick_cont.breadcrumb == "Guide > Install > Quick Start"
    usage = [c for c in chunks if "## Usage" in c.content or
             ("Use it like so" in c.content and "Run this" not in c.content)]
    assert usage
    assert all(c.breadcrumb == "Guide > Usage" for c in usage)
    # Exact full-path check: a chunk starting inside the deepest section
    doc = "# A\n\n## B\n\n### C\n\n" + "deep text. " * 30 + "\n"
    deep = _chunker(120, 0).chunk(doc)
    assert deep[0].breadcrumb == "A"
    assert deep[-1].breadcrumb == "A > B > C"


def test_breadcrumb_uses_own_content_not_overlap():
    """A chunk whose overlap comes from section A but whose own text starts
    section B is labelled B."""
    chunker = _chunker(250, 80)
    chunks = chunker.chunk(MD_DOC)
    usage_chunks = [c for c in chunks if "## Usage" in c.content]
    assert usage_chunks
    for c in usage_chunks:
        if c.content.lstrip().startswith("## Usage") or not c.content.startswith("## Usage"):
            # own content starts at the heading even if overlap precedes it
            assert c.breadcrumb == "Guide > Usage"


def test_atx_closing_hashes_and_whitespace_are_cleaned():
    chunker = _chunker(1000, 0)
    [c] = chunker.chunk("##   Spaced   Title   ##\n\nbody\n")
    assert c.breadcrumb == "Spaced Title"


def test_text_before_first_heading_has_empty_breadcrumb():
    chunker = _chunker(100, 0)
    text = "Lead-in words. " * 10 + "\n\n# Heading\n\n" + "Body words. " * 10 + "\n"
    chunks = chunker.chunk(text)
    assert chunks[0].breadcrumb == ""
    assert chunks[-1].breadcrumb == "Heading"


def test_single_chunk_document_gets_breadcrumb():
    [c] = _chunker(1000, 0).chunk("# Only\n\nshort\n")
    assert c.breadcrumb == "Only"


def test_chunk_content_is_never_prefixed_with_breadcrumb():
    chunker = _chunker(250, 40)
    for c in chunker.chunk(MD_DOC):
        assert c.content in MD_DOC
        assert not c.content.startswith("Guide >")


# ---------------------------------------------------------------------------
# Storage policy: breadcrumb as metadata, only for md/txt/rst
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["README.md", "docs/a.MD", "notes.txt", "spec.rst", "x.markdown"])
def test_breadcrumbs_enabled_for_document_types(path):
    assert breadcrumbs_enabled(path)


@pytest.mark.parametrize("path", ["main.py", "lib.c", "header.h", "cfg.yaml", "Makefile", "script.sh"])
def test_breadcrumbs_disabled_for_other_types(path):
    assert not breadcrumbs_enabled(path)


def test_rst_not_in_default_index_extensions():
    """Per requirements: .rst gets breadcrumbs but is NOT auto-indexed."""
    from rag_mcp.config_loader import DEFAULT_INDEX_EXTENSIONS

    assert ".rst" not in {e.ext for e in DEFAULT_INDEX_EXTENSIONS}
    assert ".rst" in BREADCRUMB_EXTENSIONS


def test_chunk_metadatas_respects_policy():
    chunks = [Chunk("a", 0, 2, breadcrumb="Top > Sub"), Chunk("b", 1, 2, breadcrumb="")]
    assert chunk_metadatas(chunks, "doc.md") == [{"breadcrumb": "Top > Sub"}, {"breadcrumb": ""}]
    assert chunk_metadatas(chunks, "code.py") == [{"breadcrumb": ""}, {"breadcrumb": ""}]


def test_pipeline_stores_breadcrumb_metadata_per_chunk():
    """_process_file passes a per-chunk breadcrumb to the store; content untouched."""
    from rag_mcp.config_loader import (
        AppConfig, EmbeddingConfig, SourcePattern, StorageConfig,
    )
    from rag_mcp.file_reader import FileContent
    from rag_mcp.indexing_pipeline import IndexingPipeline

    config = AppConfig(
        embedding=EmbeddingConfig(), storage=StorageConfig(path="/nonexistent"),
        chunking=ChunkingConfig(chunk_size=250, chunk_overlap=40), projects=[],
    )
    store = MagicMock()
    store.get_existing_hash.return_value = None
    embedding_gen = MagicMock()
    embedding_gen.encode.side_effect = lambda texts: [[0.0] * 4 for _ in texts]
    pipeline = IndexingPipeline(
        config=config, file_reader=MagicMock(), chunker=Chunker(config.chunking),
        embedding_gen=embedding_gen, store=store, console=MagicMock(),
    )
    fc = FileContent(path=Path("/b/guide.md"), relative_path="guide.md", content=MD_DOC, file_hash="h")
    source = SourcePattern(pattern="**/*.md", type="documentation")

    n = pipeline._process_file(MagicMock(), fc, source, "proj")

    assert n > 1
    args = store.upsert_chunks.call_args.args
    chunk_texts, per_chunk = args[2], args[5]
    assert len(per_chunk) == len(chunk_texts) == n
    assert all(set(m) == {"breadcrumb"} for m in per_chunk)
    assert any(m["breadcrumb"] == "Guide > Install > Quick Start" for m in per_chunk)
    # Embedded/stored text is the raw chunk: no breadcrumb prefix
    embedded = embedding_gen.encode.call_args.args[0]
    assert embedded == chunk_texts
    assert all(t in MD_DOC for t in chunk_texts)


def test_store_merges_per_chunk_metadata():
    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.config_loader import StorageConfig

    store = ChromaStore(StorageConfig(path="unused"))
    collection = MagicMock()
    store.upsert_chunks(
        collection, "guide.md", ["a", "b"], [[0.0], [0.0]],
        {"file_path": "guide.md", "file_type": "documentation"},
        [{"breadcrumb": "Top"}, {"breadcrumb": ""}],
    )
    metas = collection.upsert.call_args.kwargs["metadatas"]
    assert metas[0] == {
        "file_path": "guide.md", "file_type": "documentation",
        "breadcrumb": "Top", "chunk_index": 0, "total_chunks": 2,
    }
    assert metas[1]["breadcrumb"] == ""

    with pytest.raises(ValueError):
        store.upsert_chunks(collection, "g.md", ["a", "b"], [[0.0], [0.0]], {}, [{"breadcrumb": "x"}])


def test_store_without_per_chunk_metadata_is_unchanged():
    from rag_mcp.chroma_store import ChromaStore
    from rag_mcp.config_loader import StorageConfig

    store = ChromaStore(StorageConfig(path="unused"))
    collection = MagicMock()
    store.upsert_chunks(collection, "f.py", ["a"], [[0.0]], {"file_path": "f.py"})
    metas = collection.upsert.call_args.kwargs["metadatas"]
    assert metas == [{"file_path": "f.py", "chunk_index": 0, "total_chunks": 1}]
