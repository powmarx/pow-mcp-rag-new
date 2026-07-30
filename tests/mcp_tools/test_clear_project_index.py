"""
Tests for clear_project_index MCP tool.

Verifies that:
1. Clearing an existing project deletes its ChromaDB collection
2. The project remains in config.yaml after clearing
3. Clearing a non-existent project returns an error
4. Empty name returns an error
5. Clearing a project with no collection reports 0 chunks
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

from rag_mcp.tools.management import clear_project_index


class TestClearProjectIndexValidation:
    """Tests for input validation."""

    def test_empty_name_returns_error(self):
        result = clear_project_index(name="")
        assert "Error" in result
        assert "required" in result

    def test_whitespace_name_returns_error(self):
        result = clear_project_index(name="   ")
        assert "Error" in result

    def test_nonexistent_project_returns_error(self):
        result = clear_project_index(name="nonexistent_project_xyz")
        assert "Error" in result
        assert "not found" in result


@pytest.mark.usefixtures("isolated_server_context")
class TestClearProjectIndexExecution:
    """Tests for actual index clearing.

    Every test in this class receives an isolated copy of
    ``server.config.projects`` via the ``isolated_server_context`` autouse
    fixture defined in conftest.py.  All ``server.store.*`` and
    ``server.loader.*`` stubs are applied through ``monkeypatch`` so every
    side-effect is undone unconditionally after each test.
    """

    def test_clears_collection_and_keeps_config(self, monkeypatch):
        """Clearing should delete chunks but keep project in config."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-clearable-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="Test project to clear",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        mock_collection = MagicMock()
        mock_collection.count.return_value = 150
        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=mock_collection))
        monkeypatch.setattr(server.store, "delete_collection", MagicMock())

        result = clear_project_index(name=proj_name)

        # Project still in config
        assert any(p.name == proj_name for p in server.config.projects)

        # Collection was deleted
        server.store.delete_collection.assert_called_once_with(proj_name)

        # Response shows chunks deleted
        assert "150" in result
        assert "remains in config" in result

    def test_clears_project_with_no_collection(self, monkeypatch):
        """Should handle project with no existing collection gracefully."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-no-coll-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="No collection",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=None))

        result = clear_project_index(name=proj_name)

        assert "0" in result
        assert "remains in config" in result

    def test_delete_error_returns_error_message(self, monkeypatch):
        """If ChromaDB delete fails, should return error."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-del-fail-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="Delete will fail",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        mock_collection = MagicMock()
        mock_collection.count.return_value = 10
        monkeypatch.setattr(server.store, "get_collection", MagicMock(return_value=mock_collection))
        monkeypatch.setattr(
            server.store, "delete_collection", MagicMock(side_effect=Exception("db locked"))
        )

        result = clear_project_index(name=proj_name)

        assert "Error" in result
        assert "db locked" in result

    def test_does_not_save_config(self, monkeypatch):
        """clear_project_index should NOT modify config.yaml."""
        from rag_mcp.config_loader import ProjectConfig

        proj_name = f"test-no-save-{uuid.uuid4().hex[:8]}"
        fake_project = ProjectConfig(
            name=proj_name,
            description="Should not save",
            base_path="/tmp/fake",
        )
        server.config.projects.append(fake_project)

        monkeypatch.setattr(
            server.store,
            "get_collection",
            MagicMock(return_value=MagicMock(count=MagicMock(return_value=5))),
        )
        monkeypatch.setattr(server.store, "delete_collection", MagicMock())
        monkeypatch.setattr(server.loader, "save", MagicMock())

        clear_project_index(name=proj_name)

        # save should NOT be called
        server.loader.save.assert_not_called()
