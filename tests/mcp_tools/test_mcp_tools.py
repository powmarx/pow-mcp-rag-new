"""
Integration tests for MCP server tools.

Tests that exercise project-dependent searches use the ``two_indexed_projects``
fixture, which creates two throwaway projects under ``tmp_path``, indexes them
with known content (a source file embedding a fixture-defined function name and
a fixture-defined hex token), and tears both down unconditionally on completion.

Tests that only exercise input-validation paths (empty strings, nonexistent
projects, etc.) remain plain functions with no fixture dependency.
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

print("Loading MCP server (model + ChromaDB)...")
sys.argv.append("--no-reindex")
import server

from rag_mcp.tools.search import _search_docs_sync, _search_code_sync, _find_function_sync
from rag_mcp.tools.search import search_hex_pattern
from rag_mcp.tools.documents import get_project_summary, _compare_projects_sync
from rag_mcp.tools.management import _add_project_sync

print("Server loaded. Running tests...\n")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def two_indexed_projects(tmp_path):
    """
    Create and index two throwaway projects under tmp_path.

    Each project contains a Python source file that embeds:
    - A unique function name (``SAMPLE_FUNCTION``) so ``find_function`` and
      ``search_code`` can find it.
    - A unique hex-looking token (``SAMPLE_HEX_CODE``) so ``search_hex_pattern``
      can find it.

    Yields a namedtuple-like object with attributes:
        name_a, name_b   — project names (UUID-suffixed, unique per test run)
        function_name    — the function name embedded in both source files
        hex_code         — the hex token embedded in both source files

    Teardown removes both projects' config entries and ChromaDB collections
    unconditionally (pass, fail, or error).
    """
    suffix = uuid.uuid4().hex[:8]
    name_a = f"test-mcp-tools-a-{suffix}"
    name_b = f"test-mcp-tools-b-{suffix}"
    function_name = f"MyFixtureFunc_{suffix}"
    hex_code = f"AA{suffix[:6].upper()}"

    # Ensure embedding model is ready before indexing
    server._ensure_model_loaded()

    projects_created = []

    for proj_name in (name_a, name_b):
        proj_dir = tmp_path / proj_name
        proj_dir.mkdir()

        # Write a .py source file containing both the function name and hex token
        (proj_dir / "module.py").write_text(
            f"# Project {proj_name}\n"
            f"# Error code: {hex_code}\n\n"
            f"def {function_name}(x):\n"
            f"    \"\"\"A sample function for testing.\"\"\"\n"
            f"    return x * 2\n\n"
            f"CONSTANT_{suffix.upper()} = 0x{hex_code}\n",
            encoding="utf-8",
        )
        # Also write a .md doc file so the project has documentation content
        (proj_dir / "README.md").write_text(
            f"# {proj_name}\n\n"
            f"This project exposes `{function_name}` for testing.\n"
            f"See error code {hex_code} for details.\n",
            encoding="utf-8",
        )

        result = _add_project_sync(proj_name, str(proj_dir))
        assert "added and indexed successfully" in result, (
            f"two_indexed_projects fixture: failed to add {proj_name}:\n{result}"
        )
        projects_created.append(proj_name)

    class Projects:
        def __init__(self):
            self.name_a = name_a
            self.name_b = name_b
            self.function_name = function_name
            self.hex_code = hex_code

    try:
        yield Projects()
    finally:
        for proj_name in projects_created:
            try:
                server.store.delete_collection(proj_name)
            except Exception:
                pass
            to_remove = [p for p in server.config.projects if p.name == proj_name]
            for p in to_remove:
                server.config.projects.remove(p)
        try:
            server.loader.save(server.config)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# search_specs / search_docs
# ---------------------------------------------------------------------------

def test_search_specs_returns_documentation(two_indexed_projects):
    """search_specs should return results when docs exist in the indexed projects."""
    proj = two_indexed_projects
    result = _search_docs_sync(
        proj.function_name, project=proj.name_a, file_type="documentation"
    )
    assert not result.startswith("Error"), f"Unexpected error: {result}"
    assert "Found" in result, f"Expected 'Found' in result:\n{result}"


def test_search_specs_empty_query():
    """search_specs with empty query should return error."""
    result = _search_docs_sync("", file_type="documentation")
    assert "Error" in result or "empty" in result.lower()


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

def test_search_code_returns_source(two_indexed_projects):
    """search_code should return source/header results for a known function."""
    proj = two_indexed_projects
    result = _search_code_sync(proj.function_name, project=proj.name_a)
    assert not result.startswith("Error"), f"Unexpected error: {result}"
    assert (
        "Result" in result or "source" in result.lower() or "header" in result.lower()
    ), f"Expected result markers:\n{result}"


def test_search_code_headers_only(two_indexed_projects):
    """search_code with headers_only should still return results or a clean no-match."""
    proj = two_indexed_projects
    # The fixture only creates .py and .md files, not .h headers, so we may get
    # no header results — that's acceptable; what must NOT happen is an error.
    result = _search_code_sync(proj.function_name, project=proj.name_a, headers_only=True)
    assert not result.startswith("Error"), f"Unexpected error: {result}"


# ---------------------------------------------------------------------------
# find_function
# ---------------------------------------------------------------------------

def test_find_function_known(two_indexed_projects):
    """find_function should find the function embedded in the fixture source file."""
    proj = two_indexed_projects
    result = _find_function_sync(proj.function_name, project=proj.name_a)
    assert not result.startswith("Error"), f"Tool returned error: {result[:100]}"
    assert proj.function_name in result
    assert (
        "Files containing" in result or "Result" in result or "Declarations" in result
    ), f"Expected result markers:\n{result}"


def test_find_function_empty():
    """find_function with empty name should return error."""
    result = _find_function_sync("")
    assert "Error" in result


# ---------------------------------------------------------------------------
# compare_projects
# ---------------------------------------------------------------------------

def test_compare_projects_both(two_indexed_projects):
    """compare_projects should show results from both projects."""
    proj = two_indexed_projects
    result = _compare_projects_sync(proj.function_name, proj.name_a, proj.name_b)
    assert "PROJECT A" in result
    assert "PROJECT B" in result
    assert proj.name_a in result
    assert proj.name_b in result


def test_compare_projects_missing_project(two_indexed_projects):
    """compare_projects with one missing project should show an error in that section."""
    proj = two_indexed_projects
    result = _compare_projects_sync("some query", proj.name_a, "NonExistentProject")
    assert "PROJECT A" in result
    assert "PROJECT B" in result
    assert "not found" in result.lower() or "Error" in result


def test_compare_projects_empty_query():
    """compare_projects with empty query should return error."""
    result = _compare_projects_sync("", "A", "B")
    assert "Error" in result


# ---------------------------------------------------------------------------
# get_project_summary
# ---------------------------------------------------------------------------

def test_get_project_summary_a(two_indexed_projects):
    """get_project_summary should return stats for project A."""
    proj = two_indexed_projects
    result = get_project_summary(proj.name_a)
    assert proj.name_a in result
    assert "Total files:" in result
    assert "Total chunks:" in result


def test_get_project_summary_missing():
    """get_project_summary with missing project should return error."""
    result = get_project_summary("NonExistent")
    assert "Error" in result or "not found" in result.lower()


def test_get_project_summary_empty():
    """get_project_summary with empty name should return error."""
    result = get_project_summary("")
    assert "Error" in result


# ---------------------------------------------------------------------------
# search_hex_pattern
# ---------------------------------------------------------------------------

def test_search_hex_pattern_found(two_indexed_projects):
    """search_hex_pattern should find the hex token embedded in fixture files."""
    proj = two_indexed_projects
    result = search_hex_pattern(proj.hex_code, project=proj.name_a)
    assert "Match" in result, f"Expected 'Match' in result:\n{result}"
    assert proj.hex_code.upper() in result.upper()


def test_search_hex_pattern_with_0x_prefix():
    """search_hex_pattern should handle 0x prefix without crashing."""
    result = search_hex_pattern("0x0521")
    assert not result.startswith("Error")


def test_search_hex_pattern_empty():
    """search_hex_pattern with empty pattern should return error."""
    result = search_hex_pattern("")
    assert "Error" in result


def test_search_hex_pattern_not_found(two_indexed_projects):
    """search_hex_pattern with non-existent pattern should return no matches.

    Scoped to a project from the fixture rather than searching all collections:
    on a fresh checkout with no other projects indexed, an unscoped search has
    zero collections to search at all and returns "No indexed collections
    found." instead of "No matches" — this isn't a "no environment dependency"
    case after all, it just needs *some* indexed collection to exist.
    """
    proj = two_indexed_projects
    result = search_hex_pattern("ZZZZZZZZ", project=proj.name_a)
    assert "No matches" in result


# ---------------------------------------------------------------------------
# add_project
# ---------------------------------------------------------------------------

def test_add_project_invalid_path():
    """add_project with non-existent path should return error."""
    result = _add_project_sync("test-invalid", "C:/nonexistent/path/xyz")
    assert "Error" in result
    assert "does not exist" in result


def test_add_project_empty_name():
    """add_project with empty name should return error."""
    result = _add_project_sync("", "C:/some/path")
    assert "Error" in result


def test_add_project_duplicate(two_indexed_projects):
    """add_project with an existing project name should return error."""
    proj = two_indexed_projects
    result = _add_project_sync(proj.name_a, "C:/some/path")
    assert "Error" in result or "already exists" in result.lower()
