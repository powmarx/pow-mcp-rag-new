"""
Path resolution for rag-mcp.

Priority (highest to lowest):
  1. RAG_CONFIG_PATH env var  — explicit override (Docker volume, CI)
  2. ~/.config/rag-mcp/config.yaml  — XDG user config (pip install)
  3. <repo>/config/config.template.yaml  — bundled template (seed only)

Data directory (ChromaDB storage):
  1. RAG_DATA_PATH env var
  2. ~/.local/share/rag-mcp/  — XDG user data
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "rag-mcp"


def xdg_config_home() -> Path:
    """XDG_CONFIG_HOME or ~/.config on all platforms."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return Path(xdg)
    # Windows: use %APPDATA% as the XDG-equivalent
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata)
    return Path.home() / ".config"


def xdg_data_home() -> Path:
    """XDG_DATA_HOME or ~/.local/share (~/AppData/Local on Windows)."""
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return Path(xdg)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local)
    return Path.home() / ".local" / "share"


def default_config_path() -> Path:
    """User config file path: ~/.config/rag-mcp/config.yaml"""
    return xdg_config_home() / APP_NAME / "config.yaml"


def default_data_path() -> Path:
    """User data directory: ~/.local/share/rag-mcp/"""
    return xdg_data_home() / APP_NAME


def resolve_config_path() -> Path:
    """
    Return the active config path, seeding from the bundled template if needed.

    Priority:
      1. RAG_CONFIG_PATH env var
      2. ~/.config/rag-mcp/config.yaml (created from template on first run)
    """
    env = os.environ.get("RAG_CONFIG_PATH", "").strip()
    if env:
        return Path(env)

    cfg = default_config_path()
    if not cfg.exists():
        _seed_config(cfg)
    return cfg


def resolve_data_path() -> Path:
    """
    Return the active data (ChromaDB storage) path.

    Priority:
      1. RAG_DATA_PATH env var
      2. ~/.local/share/rag-mcp/
    """
    env = os.environ.get("RAG_DATA_PATH", "").strip()
    if env:
        return Path(env)
    return default_data_path()


def _seed_config(target: Path) -> None:
    """
    Copy config.template.yaml to the target path on first run.
    Searches relative to this file — works for repo checkout, editable
    install, and installed wheel/sdist (pip install / uvx).
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    # Find template. Priority order:
    #   1. rag_mcp/data/config.template.yaml   — bundled inside the installed
    #      package (wheel/sdist via [tool.setuptools.package-data]; kept in
    #      sync with config/ by scripts/sync_package_data.py before each build)
    #   2. <repo>/config/config.template.yaml  — repo checkout / editable install
    template = resolve_package_data_file("config.template.yaml")
    if template.exists():
        shutil.copy(template, target)
        print(f"[config] Created config at {target}", file=sys.stderr)
        print(f"[config] Edit it to add your projects, then run: rag-mcp index", file=sys.stderr)
        return

    # No template found — create minimal stub so the app can start.
    # Kept in sync with config_loader.py's EmbeddingConfig/RerankerConfig
    # dataclass defaults (BAAI/bge-small-en-v1.5 + reranker enabled) so a
    # missing template doesn't silently downgrade retrieval quality.
    target.write_text(
        "embedding:\n  model: BAAI/bge-small-en-v1.5\n"
        "reranker:\n  enabled: true\n"
        "storage:\n  path: ''\n  collection_prefix: rag\n  mode: local\n"
        "chunking:\n  chunk_size: 1000\n  chunk_overlap: 200\n"
        "projects: []\n",
        encoding="utf-8",
    )
    print(f"[config] WARNING: bundled template not found, created minimal stub at {target}", file=sys.stderr)

def resolve_package_data_file(relative_name: str) -> Path:
    """
    Locate a bundled data file by name, e.g. "config.template.yaml" or
    "detection_rules.json". Priority:
      1. Installed package data: rag_mcp/data/<relative_name>
         (works for pip/uvx installs, Docker, and any entry point — resolved
         via import, not directory-counting, so it's correct regardless of
         where the caller script itself lives on disk)
      2. Repo checkout fallback: <repo_root>/config/<relative_name>
         (only reachable if this file itself is running from src/rag_mcp/,
         i.e. editable install / repo checkout)

    Returns the first candidate that exists, or the first candidate
    (installed-package path) if neither exists, so error messages point
    somewhere meaningful.
    """
    here = Path(__file__).parent  # src/rag_mcp/
    candidates = [
        here / "data" / relative_name,
        here.parent.parent / "config" / relative_name,  # src/rag_mcp -> src -> repo root
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]
