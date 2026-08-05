"""Shared fixtures for server subprocess tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_CONFIG = REPO_ROOT / "src" / "rag_mcp" / "data" / "config.template.yaml"


@pytest.fixture
def server_subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Build deterministic env for tests that spawn ``server.py`` via stdio."""
    config_path = tmp_path / "config.yaml"
    data_path = (tmp_path / "data").as_posix()

    template = TEMPLATE_CONFIG.read_text(encoding="utf-8")
    config_text = template.replace('path: "./data"', f'path: "{data_path}"')
    config_path.write_text(config_text, encoding="utf-8")

    env = dict(os.environ)
    env["RAG_CONFIG_PATH"] = str(config_path)
    return env

