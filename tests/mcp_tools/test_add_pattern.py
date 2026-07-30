"""
Unit tests for the add_pattern management tool.

Covers:
- Happy path: pattern matches files, chunks indexed, config persisted
- No-match: pattern saved to config but no indexing performed
- Duplicate pattern: already in config, only index updated
- Invalid type: rejected with clear error
- Missing project: rejected with clear error
- Removed project: rejected with clear error
- Skipped unreadable files: counted and reported, rest indexed
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs so management.py can be imported without a live server
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path, projects: list | None = None):
    """Build a minimal ToolContext-like object for testing."""
    from rag_mcp.config_loader import AppConfig, ChunkingConfig, StorageConfig, SourcePattern
    from rag_mcp.tools import ToolContext

    if projects is None:
        from rag_mcp.config_loader import ProjectConfig
        projects = [
            ProjectConfig(
                name="test_proj",
                description="test",
                base_path=str(tmp_path),
                sources=[],
            )
        ]

    config = AppConfig(
        projects=projects,
        storage=StorageConfig(path=str(tmp_path / "data"), collection_prefix="rag"),
        chunking=ChunkingConfig(chunk_size=200, chunk_overlap=20, separators=["\n\n", "\n"]),
        embedding=MagicMock(),
    )

    mock_store = MagicMock()
    mock_collection = MagicMock()
    mock_store.get_collection.return_value = mock_collection
    mock_store.delete_file_chunks = MagicMock()
    mock_store.upsert_chunks = MagicMock()

    mock_loader = MagicMock()
    mock_loader.save = MagicMock()

    mock_embedding_gen = MagicMock()
    mock_embedding_gen.encode.return_value = [[0.1] * 384]  # one embedding per call

    ctx = ToolContext(
        config=config,
        loader=mock_loader,
        store=mock_store,
        embedding_gen=mock_embedding_gen,
        ensure_model_loaded=MagicMock(),
        reindex_in_progress=lambda: False,
        indexing_cancelled=lambda: False,
        set_indexing_cancelled=MagicMock(),
    )
    return ctx, mock_collection, mock_loader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(fn, *args, **kwargs):
    """Call a _sync implementation directly (bypass async wrapper)."""
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAddPatternHappyPath:

    def test_indexes_matching_files_and_saves_config(self, tmp_path):
        """Files matching the pattern are indexed and pattern is saved to config."""
        # Create a markdown file the pattern will match
        doc_dir = tmp_path / "doc" / "specs"
        doc_dir.mkdir(parents=True)
        (doc_dir / "guide.md").write_text("# Guide\nSome content here.\n" * 20)

        ctx, collection, loader = _make_ctx(tmp_path)

        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        result = mgmt._add_pattern_sync(
            project="test_proj",
            pattern="doc/specs/**/*.md",
            type="documentation",
            description="Spec guides",
        )

        assert "indexed" in result.lower() or "Pattern indexed" in result
        assert "doc/specs/**/*.md" in result
        assert "test_proj" in result
        # Config should have been saved with the new pattern
        loader.save.assert_called_once()
        patterns = [s.pattern for s in ctx.config.projects[0].sources]
        assert "doc/specs/**/*.md" in patterns
        # Store should have received chunks
        ctx.store.upsert_chunks.assert_called()

    def test_correct_type_stored_in_config(self, tmp_path):
        """The requested type is written into the SourcePattern in config."""
        (tmp_path / "notes.md").write_text("content\n" * 30)

        ctx, _, loader = _make_ctx(tmp_path)
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        mgmt._add_pattern_sync("test_proj", "*.md", type="documentation", description="Notes")

        source = next(s for s in ctx.config.projects[0].sources if s.pattern == "*.md")
        assert source.type == "documentation"
        assert source.description == "Notes"

    def test_default_description_when_empty(self, tmp_path):
        """A sensible default description is generated when none is given."""
        (tmp_path / "file.h").write_text("// header\n" * 10)

        ctx, _, loader = _make_ctx(tmp_path)
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        mgmt._add_pattern_sync("test_proj", "*.h", type="header", description="")

        source = next(s for s in ctx.config.projects[0].sources if s.pattern == "*.h")
        assert source.description  # non-empty
        assert "*.h" in source.description or "Manually" in source.description


class TestAddPatternNoMatch:

    def test_no_match_saves_pattern_without_indexing(self, tmp_path):
        """When no files match, the pattern is still persisted for future use."""
        ctx, _, loader = _make_ctx(tmp_path)
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        result = mgmt._add_pattern_sync(
            project="test_proj",
            pattern="nonexistent/**/*.md",
            type="documentation",
        )

        assert "no files matched" in result.lower() or "No files matched" in result or "saved to config" in result.lower()
        loader.save.assert_called_once()
        patterns = [s.pattern for s in ctx.config.projects[0].sources]
        assert "nonexistent/**/*.md" in patterns
        # No indexing should have happened
        ctx.store.upsert_chunks.assert_not_called()

    def test_no_match_duplicate_pattern_reports_already_present(self, tmp_path):
        """If pattern already exists and still no match, report without error."""
        from rag_mcp.config_loader import SourcePattern
        ctx, _, loader = _make_ctx(tmp_path)
        ctx.config.projects[0].sources.append(
            SourcePattern(pattern="nonexistent/**/*.md", type="documentation", description="old")
        )
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        result = mgmt._add_pattern_sync(
            project="test_proj",
            pattern="nonexistent/**/*.md",
            type="documentation",
        )

        # Should not error, should not duplicate the pattern
        assert "Error" not in result
        patterns = [s.pattern for s in ctx.config.projects[0].sources]
        assert patterns.count("nonexistent/**/*.md") == 1
        # No new save needed (nothing changed)
        loader.save.assert_not_called()


class TestAddPatternDuplicate:

    def test_duplicate_pattern_updates_index_without_duplicating_config(self, tmp_path):
        """Re-adding an existing pattern re-indexes files but doesn't duplicate config entry."""
        from rag_mcp.config_loader import SourcePattern

        (tmp_path / "readme.md").write_text("# README\n" * 20)

        ctx, _, loader = _make_ctx(tmp_path)
        ctx.config.projects[0].sources.append(
            SourcePattern(pattern="*.md", type="documentation", description="existing")
        )
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        result = mgmt._add_pattern_sync("test_proj", "*.md", type="documentation")

        assert "Error" not in result
        patterns = [s.pattern for s in ctx.config.projects[0].sources]
        assert patterns.count("*.md") == 1  # no duplicate
        # Index should be updated
        ctx.store.upsert_chunks.assert_called()
        # Config not saved again (pattern already there)
        loader.save.assert_not_called()


def _default_projects(tmp_path):
    """Setup factory: use `_make_ctx`'s own default single `test_proj` project."""
    return None


def _removed_projects(tmp_path):
    """Setup factory: a single project marked `removed=True` (`dead_proj`)."""
    from rag_mcp.config_loader import ProjectConfig

    return [
        ProjectConfig(
            name="dead_proj",
            description="removed",
            base_path=str(tmp_path),
            sources=[],
            removed=True,
        )
    ]


class TestAddPatternValidation:

    @pytest.mark.parametrize(
        "setup_projects, project, pattern, type, expected_substring",
        [
            (_default_projects, "test_proj", "*.md", "invalid_type", "invalid_type"),
            (_default_projects, "nonexistent_proj", "*.md", "documentation", "not found"),
            (_default_projects, "", "*.md", "documentation", "project"),
            (_default_projects, "test_proj", "", "documentation", "pattern"),
            (_removed_projects, "dead_proj", "*.md", "documentation", "removed"),
        ],
        ids=[
            "invalid_type",
            "missing_project",
            "empty_project",
            "empty_pattern",
            "removed_project",
        ],
    )
    def test_validation_errors(
        self, tmp_path, setup_projects, project, pattern, type, expected_substring
    ):
        projects = setup_projects(tmp_path)
        ctx, _, _ = _make_ctx(tmp_path, projects=projects)
        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        result = mgmt._add_pattern_sync(project, pattern, type=type)
        assert result.startswith("Error")
        assert expected_substring.lower() in result.lower()


class TestAddPatternSkipsUnreadable:

    def test_unreadable_files_skipped_rest_indexed(self, tmp_path):
        """Binary files are skipped; readable files are indexed normally."""
        # One readable markdown file
        (tmp_path / "good.md").write_text("# Content\n" * 20)
        # One binary file disguised as .md (null bytes)
        (tmp_path / "bad_bin.md").write_bytes(b"\x00\x01\x02\x03" * 50)

        ctx, _, loader = _make_ctx(tmp_path)

        # Patch at the module level used by _add_pattern_sync to avoid recursion
        import rag_mcp.file_reader as fr_mod

        original_read = fr_mod.FileReader.read

        def patched_read(self_fr, path, base):
            if "bad_bin" in str(path):
                return None  # simulate unreadable
            return original_read(self_fr, path, base)

        import rag_mcp.tools.management as mgmt
        mgmt._ctx = ctx

        fr_mod.FileReader.read = patched_read
        try:
            result = mgmt._add_pattern_sync("test_proj", "*.md", type="documentation")
        finally:
            fr_mod.FileReader.read = original_read

        # Should not error
        assert "Error" not in result
        # Good file was indexed
        ctx.store.upsert_chunks.assert_called()
