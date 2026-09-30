"""Tests for add-project source pattern fallback behavior."""

import sys
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp._indexer import _handle_add_project
from rag_mcp.config_loader import ConfigLoader


def _write_config(path: Path) -> None:
    path.write_text(
        """
embedding:
  model: "sentence-transformers/all-MiniLM-L6-v2"

storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
  url: ""

chunking:
  chunk_size: 500
  chunk_overlap: 100

projects: []

index_extensions:
  - ext: ".pdf"
    type: "documentation"
    description: "PDF docs/specs"
  - ext: ".md"
    type: "documentation"
    description: "Markdown docs/specs"
  - ext: ".py"
    type: "source"
    description: "Python sources"
""".strip(),
        encoding="utf-8",
    )


def test_add_project_falls_back_to_all_index_extensions(tmp_path):
    """When auto-detection yields no patterns, add-project should use all index_extensions."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    loader = ConfigLoader(config_path)
    config = loader.load()

    project_root = tmp_path / "docs_only"
    project_root.mkdir()
    (project_root / "manual.pdf").write_bytes(b"%PDF-1.4 test")

    args = SimpleNamespace(name="docs-fallback", path=str(project_root))
    console = Console(record=True, highlight=False, force_terminal=False)
    _handle_add_project(args, config, loader, console)

    reloaded = loader.load()
    project = next(p for p in reloaded.projects if p.name == "docs-fallback")

    assert [s.pattern for s in project.sources] == ["**/*.pdf", "**/*.md", "**/*.py"]
    assert [s.type for s in project.sources] == ["documentation", "documentation", "source"]
    assert [s.description for s in project.sources] == [
        "PDF docs/specs",
        "Markdown docs/specs",
        "Python sources",
    ]


def test_add_project_keeps_detected_patterns_when_non_empty(tmp_path):
    """When auto-detection finds patterns, add-project should not use fallback extensions."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    loader = ConfigLoader(config_path)
    config = loader.load()

    project_root = tmp_path / "python_project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project_root / "main.py").write_text("print('ok')\n", encoding="utf-8")

    args = SimpleNamespace(name="python-detected", path=str(project_root))
    console = Console(record=True, highlight=False, force_terminal=False)
    _handle_add_project(args, config, loader, console)

    reloaded = loader.load()
    project = next(p for p in reloaded.projects if p.name == "python-detected")
    patterns = [s.pattern for s in project.sources]

    assert "**/*.py" in patterns
    assert "**/*.pdf" not in patterns
