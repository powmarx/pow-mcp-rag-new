"""Integration tests for MCP tools driven by JSON casepacks."""

import sys
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
assert (_REPO_ROOT / "pyproject.toml").exists(), (
    f"_REPO_ROOT did not resolve to the repo root: {_REPO_ROOT}"
)
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

print("Loading MCP server (model + ChromaDB)...")
sys.argv.append("--no-reindex")
import server

from rag_mcp.tools.management import _add_project_sync
from tests.runners.casepacks import load_cases
from tests.runners.dispatch import run_case

print("Server loaded. Running tests...\n")

INTEGRATION_CASES = load_cases("mcp_tools.integration.json")
INTEGRATION_CASES_WITH_FIXTURE = [c for c in INTEGRATION_CASES if c.get("requires_fixture")]
INTEGRATION_CASES_WITHOUT_FIXTURE = [c for c in INTEGRATION_CASES if not c.get("requires_fixture")]


@pytest.fixture()
def two_indexed_projects(tmp_path):
    """Create and index two throwaway projects under tmp_path."""
    suffix = uuid.uuid4().hex[:8]
    name_a = f"test-mcp-tools-a-{suffix}"
    name_b = f"test-mcp-tools-b-{suffix}"
    function_name = f"MyFixtureFunc_{suffix}"
    hex_code = f"AA{suffix[:6].upper()}"

    server._ensure_model_loaded()

    projects_created = []
    for proj_name in (name_a, name_b):
        proj_dir = tmp_path / proj_name
        proj_dir.mkdir()
        (proj_dir / "module.py").write_text(
            f"# Project {proj_name}\n"
            f"# Error code: {hex_code}\n\n"
            f"def {function_name}(x):\n"
            f"    \"\"\"A sample function for testing.\"\"\"\n"
            f"    return x * 2\n\n"
            f"CONSTANT_{suffix.upper()} = 0x{hex_code}\n",
            encoding="utf-8",
        )
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


@pytest.mark.parametrize(
    "case",
    INTEGRATION_CASES_WITHOUT_FIXTURE,
    ids=[case["id"] for case in INTEGRATION_CASES_WITHOUT_FIXTURE],
)
def test_casepack_integration_without_fixture(case):
    run_case(case)


@pytest.mark.parametrize(
    "case",
    INTEGRATION_CASES_WITH_FIXTURE,
    ids=[case["id"] for case in INTEGRATION_CASES_WITH_FIXTURE],
)
def test_casepack_integration_with_fixture(case, two_indexed_projects):
    run_case(case, context={"fixture": vars(two_indexed_projects)})

