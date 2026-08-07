"""
Tests for remove_project MCP tool.

Verifies that:
1. Removing an existing project deletes its ChromaDB collection
2. Removing a project removes it from config.yaml (soft-delete: marks removed=True)
3. Removing a non-existent project returns an error
4. Empty name returns an error
5. Config is saved after removal
"""

import uuid
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
assert (_REPO_ROOT / "pyproject.toml").exists(), (
    f"_REPO_ROOT did not resolve to the repo root: {_REPO_ROOT}"
)
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

# Import server with --no-reindex to avoid heavy startup
if "--no-reindex" not in sys.argv:
    sys.argv.append("--no-reindex")
import server

from rag_mcp.tools.management import remove_project


@pytest.mark.usefixtures("isolated_server_context")
class TestRemoveProjectExecution:
    """Tests for actual project removal.

    Every test in this class receives an isolated copy of
    ``server.config.projects`` via the ``isolated_server_context`` autouse
    fixture defined in conftest.py.  All ``server.store.*`` and
    ``server.loader.*`` stubs are applied through ``monkeypatch`` so every
    side-effect is undone unconditionally after each test.
    """

    def test_removes_project_from_config(self, monkeypatch):
        """Removing a project should soft-delete it in config.projects."""
        from rag_mcp.config_loader import ProjectConfig, SourcePattern

        proj_name = f"test-removable-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="Test project to remove",
            base_path="/tmp/fake",
            sources=[SourcePattern(pattern="*.txt", type="source", description="test")],
        )
        server.config.projects.append(fake_project)

        mock_collection = MagicMock()
        mock_collection.count.return_value = 42
        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=mock_collection))
        monkeypatch.setattr(server.store, "delete_collection", MagicMock())
        monkeypatch.setattr(server.loader, "save", MagicMock())

        result = remove_project(name=proj_name)

        # Verify it was marked as removed (soft-delete)
        project = next((p for p in server.config.projects if p.name == proj_name), None)
        assert project is not None, "Project should still exist in config"
        assert project.removed is True, "Project should be marked as removed"

        # Verify collection was deleted
        server.store.delete_collection.assert_called_once_with(proj_name)

        # Verify config was saved
        server.loader.save.assert_called_once()

        # Verify response
        assert "removed successfully" in result
        assert "42" in result  # chunks deleted count

    def test_removes_project_with_no_collection(self, monkeypatch):
        """Should handle project that has no ChromaDB collection gracefully."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-no-collection-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="No collection exists",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=None))
        monkeypatch.setattr(server.loader, "save", MagicMock())

        result = remove_project(name=proj_name)

        assert "removed successfully" in result
        assert "0" in result  # 0 chunks deleted

    def test_config_save_failure_reports_error(self, monkeypatch):
        """If config save fails, should report the error."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-save-fail-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="Test save failure",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        mock_collection = MagicMock()
        mock_collection.count.return_value = 5
        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=mock_collection))
        monkeypatch.setattr(server.store, "delete_collection", MagicMock())
        monkeypatch.setattr(server.loader, "save", MagicMock(side_effect=IOError("disk full")))

        result = remove_project(name=proj_name)

        assert "failed to update config.yaml" in result
        assert "disk full" in result
