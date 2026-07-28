"""
rag-mcp — CLI entry point.

Usage:
    rag-mcp serve                # stdio MCP server (default)
    rag-mcp serve --http         # Streamable HTTP MCP server
    rag-mcp serve --no-reindex   # stdio, skip background reindex
    rag-mcp index                # index all configured projects
    rag-mcp index --project NAME # index one project
    rag-mcp index --reset        # clear + full re-index
    rag-mcp index --prune        # remove stale chunks
    rag-mcp index --estimate     # dry-run size estimate
    rag-mcp config               # show resolved config path
    rag-mcp config --init        # seed config from template and exit
    rag-mcp docs                 # list bundled docs
    rag-mcp docs cli              # print the full CLI reference
    rag-mcp docs <name>          # print a bundled doc to stdout
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bundled inside the wheel via [tool.setuptools.package-data] in pyproject.toml
# (kept in sync with doc/ via scripts/sync_package_data.py). Only docs useful
# without a repo checkout are bundled — DOCKER_GUIDE/ARCHITECTURE/
# PIP_INSTALL_GUIDE/TROUBLESHOOTING stay repo-only (setup-time concerns).
_BUNDLED_DOCS = {
    "tools": "TOOLS_GUIDE.md",
    "log-indexing": "LOG_INDEXING_GUIDE.md",
    "log-patterns": "LOG_PATTERN_CONFIGURATION.md",
    "cli": "CLI_REFERENCE.md",
}


def _ensure_data_path_in_config(config_path: Path, data_path: Path) -> None:
    """
    If the config's storage.path is empty or relative, replace it with
    the resolved XDG data path so the app has a writable ChromaDB location.
    Only applied when running as an installed package (not Docker/env-var mode).
    """
    import os
    # Don't touch if RAG_DATA_PATH or RAG_CONFIG_PATH is explicitly set
    if os.environ.get("RAG_DATA_PATH") or os.environ.get("RAG_CONFIG_PATH"):
        return

    import yaml
    text = config_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    current = str(cfg.get("storage", {}).get("path", "")).strip()
    if not current or not Path(current).is_absolute():
        # Patch the in-memory config; do NOT rewrite the file
        # (server.py / indexer.py will use the patched value via env var)
        data_path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("RAG_DATA_PATH", str(data_path))


def _docs_dir() -> Path:
    """Locate the bundled docs folder: rag_mcp/data/docs/ (installed package)
    or <repo>/doc/ (repo checkout / editable install, mirrors paths.py's
    template lookup order)."""
    here = Path(__file__).parent
    candidates = [
        here / "data" / "docs",
        here.parent.parent / "doc",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]  # fall through to a clear "not found" error later


def _docs_main(args: list[str]) -> None:
    docs_dir = _docs_dir()
    rest = args[1:]

    if not rest:
        print("Available docs (rag-mcp docs <name>):\n")
        for name, filename in _BUNDLED_DOCS.items():
            exists = (docs_dir / filename).exists()
            marker = "" if exists else "  [missing]"
            print(f"  {name:<14} {filename}{marker}")
        print(f"\nDocs directory: {docs_dir}")
        return

    name = rest[0]
    filename = _BUNDLED_DOCS.get(name)
    if not filename:
        print(f"Unknown doc: '{name}'", file=sys.stderr)
        print(f"Available: {', '.join(_BUNDLED_DOCS)}", file=sys.stderr)
        sys.exit(1)

    doc_path = docs_dir / filename
    if not doc_path.exists():
        print(f"Doc not found on disk: {doc_path}", file=sys.stderr)
        sys.exit(1)

    content = doc_path.read_text(encoding="utf-8")
    # Windows consoles default stdout to cp1252/cp850 (not UTF-8), which
    # raises UnicodeEncodeError on characters like "→" that these docs use.
    # Write raw UTF-8 bytes directly to the underlying buffer instead of
    # going through print()'s text-mode encoding.
    try:
        sys.stdout.write(content)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(content.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")


def main() -> None:
    """CLI dispatcher — routes subcommands to server or indexer logic."""
    from rag_mcp.paths import resolve_config_path, resolve_data_path

    args = sys.argv[1:]

    # --- docs subcommand ---
    if args and args[0] == "docs":
        _docs_main(args)
        return

    # --- config subcommand ---
    if args and args[0] == "config":
        cfg = resolve_config_path()
        data = resolve_data_path()
        print(f"Config : {cfg}")
        print(f"Data   : {data}")
        if "--init" in args:
            print("Config file seeded (or already exists).")
        return

    # --- index subcommand ---
    if not args or args[0] == "index":
        cfg_path = resolve_config_path()
        data_path = resolve_data_path()
        _ensure_data_path_in_config(cfg_path, data_path)

        import os
        os.environ.setdefault("RAG_CONFIG_PATH", str(cfg_path))

        # Delegate to indexer main, stripping the 'index' subcommand token
        if args and args[0] == "index":
            sys.argv = [sys.argv[0]] + args[1:]
        else:
            sys.argv = [sys.argv[0]]

        from rag_mcp._indexer import main as indexer_main
        indexer_main()
        return

    # --- serve subcommand (default) ---
    if args[0] == "serve":
        cfg_path = resolve_config_path()
        data_path = resolve_data_path()
        _ensure_data_path_in_config(cfg_path, data_path)

        import os
        os.environ.setdefault("RAG_CONFIG_PATH", str(cfg_path))

        # Pass remaining args to server (--http, --no-reindex, --port, etc.)
        sys.argv = [sys.argv[0]] + args[1:]
        from rag_mcp._server import main as server_main
        server_main()
        return

    # --- fallback: unknown subcommand ---
    print(
        "Usage: rag-mcp <command> [options]\n"
        "\n"
        "Commands:\n"
        "  serve    Start the MCP server (default: stdio transport)\n"
        "  index    Index project files into ChromaDB\n"
        "  config   Show resolved config and data paths\n"
        "  docs     List or print bundled documentation (tools, log-indexing, log-patterns)\n"
        "\n"
        "Run 'rag-mcp <command> --help' for command-specific options.\n",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
