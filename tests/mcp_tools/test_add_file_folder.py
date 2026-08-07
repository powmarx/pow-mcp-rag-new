"""
Tests for add_file and add_folder MCP tools.

Each test that exercises valid-project behaviour uses the `indexed_project` fixture,
which builds a throwaway project under pytest's `tmp_path`, indexes it with a
UUID-suffixed unique name, and tears it down unconditionally (regardless of
pass/fail) after every test function.
"""

import uuid
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
assert (_REPO_ROOT / "pyproject.toml").exists(), (
    f"_REPO_ROOT did not resolve to the repo root: {_REPO_ROOT}"
)
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

import pytest

print("Loading MCP server...")
sys.argv.append("--no-reindex")
import server

from rag_mcp.tools.management import _add_file_sync, _add_folder_sync, _add_project_sync

print("Server loaded. Running tests...\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def indexed_project(tmp_path):
    """
    Create a throwaway project under tmp_path, index it, yield (name, directory),
    and remove it on teardown regardless of whether the test passed or failed.

    The fixture:
    - Creates a unique project name (UUID-suffixed) so parallel test runs can't clash.
    - Writes at least one .md file so auto-detection finds content to index.
    - Calls _add_project_sync to register and index the project.
    - Asserts the add call succeeded before yielding.
    - On teardown: removes the project from server.config.projects and deletes
      its ChromaDB collection via server.store.delete_collection.
    """
    project_name = f"test-add-file-folder-{uuid.uuid4().hex[:8]}"
    project_dir = tmp_path / project_name
    project_dir.mkdir()

    # Write a minimal .md file so the auto-detector finds at least one source pattern.
    (project_dir / "README.md").write_text(
        "# Test project\n\nThis project was created by the pytest fixture.\n",
        encoding="utf-8",
    )

    # Ensure the embedding model is loaded before calling _add_project_sync.
    # When other test modules run first and import server without --no-reindex, the
    # model may not have been loaded synchronously; _ensure_model_loaded() blocks
    # until it is ready, preventing a RuntimeError inside the indexing pipeline.
    server._ensure_model_loaded()

    # Index the project via the same sync helper that the production code uses.
    result = _add_project_sync(project_name, str(project_dir))
    assert "added and indexed successfully" in result, (
        f"indexed_project fixture: _add_project_sync failed:\n{result}"
    )

    try:
        yield project_name, project_dir
    finally:
        # --- Unconditional teardown ---
        # 1. Delete the ChromaDB collection (best-effort; ignore if already gone).
        try:
            server.store.delete_collection(project_name)
        except Exception:
            pass

        # 2. Remove the project entry from the in-memory config list so subsequent
        #    tests start clean. IMPORTANT: mutate the list in-place rather than
        #    reassigning server.config.projects — reassignment would break the
        #    identity between server.config.projects and _ctx.config.projects inside
        #    management.py, causing "project not found" errors in tests that run after
        #    this fixture tears down.
        to_remove = [p for p in server.config.projects if p.name == project_name]
        for p in to_remove:
            server.config.projects.remove(p)
        try:
            server.loader.save(server.config)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# add_file tests
# ---------------------------------------------------------------------------

def test_add_file_valid(indexed_project):
    """add_file with a valid file should index and persist."""
    project_name, project_dir = indexed_project
    test_file = str(project_dir / "README.md")

    result = _add_file_sync(test_file, project_name)

    assert not result.startswith("Error"), f"Expected success, got: {result}"
    assert "indexed" in result.lower(), f"Expected 'indexed' in result: {result}"
    assert "Persisted" in result, f"Expected 'Persisted' in result: {result}"


def test_add_file_unknown_project(tmp_path):
    """add_file with unknown project should return error."""
    # Use a real file so the call gets past filesystem validation and reaches the
    # project-lookup step (the test is about the project-not-found path, not the
    # file-not-found path).
    real_file = tmp_path / "dummy.md"
    real_file.write_text("dummy", encoding="utf-8")

    result = _add_file_sync(str(real_file), "NonExistentProject-" + uuid.uuid4().hex[:8])

    assert result.startswith("Error"), f"Expected error, got: {result}"
    assert "not found" in result.lower(), f"Expected 'not found' in result: {result}"


# ---------------------------------------------------------------------------
# add_folder tests
# ---------------------------------------------------------------------------

def test_add_folder_valid(indexed_project):
    """add_folder with a valid folder should index and persist."""
    project_name, project_dir = indexed_project

    result = _add_folder_sync(str(project_dir), project_name, "**/*.md")

    assert not result.startswith("Error"), f"Expected success, got: {result}"
    assert "indexed" in result.lower(), f"Expected 'indexed' in result: {result}"
    assert "Persisted" in result, f"Expected 'Persisted' in result: {result}"


def test_add_folder_outside_base(indexed_project, tmp_path):
    """add_folder with folder outside project base_path should return error."""
    project_name, _project_dir = indexed_project

    # tmp_path itself is outside the indexed project's directory.
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    result = _add_folder_sync(str(outside_dir), project_name)

    assert result.startswith("Error"), f"Expected error, got: {result}"
    assert "base_path" in result, f"Expected 'base_path' in result: {result}"


def test_add_folder_no_matching_files(indexed_project):
    """add_folder with pattern that matches nothing should return error."""
    project_name, project_dir = indexed_project

    result = _add_folder_sync(str(project_dir), project_name, "**/*.xyz")

    assert result.startswith("Error"), f"Expected error, got: {result}"
    assert "No files found" in result, f"Expected 'No files found' in result: {result}"
