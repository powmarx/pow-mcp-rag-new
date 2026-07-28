"""
Sync canonical config/doc files from config/ and doc/ into src/rag_mcp/data/
so they get bundled inside the wheel (pip install / uvx mode).

config/ and doc/ remain the single sources of truth (used directly by Docker,
the native setup.bat venv mode, and the repo's own doc links). This script
keeps src/rag_mcp/data/ — the copy setuptools actually packages via
[tool.setuptools.package-data] — in sync with them.

Run this before building the wheel:
    python scripts/sync_package_data.py
    python -m build --wheel

Files synced (must stay in sync with MANIFEST.in and pyproject.toml):
    config/config.template.yaml -> src/rag_mcp/data/config.template.yaml
    config/server_info.json     -> src/rag_mcp/data/server_info.json
    config/detection_rules.json -> src/rag_mcp/data/detection_rules.json

Docs bundled for `rag-mcp-new-pip-mcp docs <name>` (useful without a repo checkout —
setup/architecture docs like DOCKER_GUIDE.md stay repo-only):
    doc/TOOLS_GUIDE.md               -> src/rag_mcp/data/docs/TOOLS_GUIDE.md
    doc/LOG_INDEXING_GUIDE.md        -> src/rag_mcp/data/docs/LOG_INDEXING_GUIDE.md
    doc/LOG_PATTERN_CONFIGURATION.md -> src/rag_mcp/data/docs/LOG_PATTERN_CONFIGURATION.md
    doc/CLI_REFERENCE.md             -> src/rag_mcp/data/docs/CLI_REFERENCE.md
"""

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_CONFIG = REPO_ROOT / "config"
SRC_DOC = REPO_ROOT / "doc"
DEST_DATA = REPO_ROOT / "src" / "rag_mcp" / "data"
DEST_DOCS = DEST_DATA / "docs"

CONFIG_FILES = [
    "config.template.yaml",
    "server_info.json",
    "detection_rules.json",
]

DOC_FILES = [
    "TOOLS_GUIDE.md",
    "LOG_INDEXING_GUIDE.md",
    "LOG_PATTERN_CONFIGURATION.md",
    "CLI_REFERENCE.md",
]


def _sync(src_dir: Path, dest_dir: Path, names: list[str]) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    synced = 0
    for name in names:
        src = src_dir / name
        if not src.exists():
            print(f"  [warn] Missing source file: {src}", file=sys.stderr)
            continue
        dest = dest_dir / name
        shutil.copy2(src, dest)
        print(f"  [sync] {src.relative_to(REPO_ROOT)} -> {dest.relative_to(REPO_ROOT)}")
        synced += 1
    return synced


def main() -> None:
    synced_config = _sync(SRC_CONFIG, DEST_DATA, CONFIG_FILES)
    synced_docs = _sync(SRC_DOC, DEST_DOCS, DOC_FILES)
    total = len(CONFIG_FILES) + len(DOC_FILES)
    print(f"\nSynced {synced_config + synced_docs}/{total} file(s) into src/rag_mcp/data/")


if __name__ == "__main__":
    main()
