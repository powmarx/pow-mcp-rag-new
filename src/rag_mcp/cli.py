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
    rag-mcp index --list-projects # list indexed projects and chunk counts
    rag-mcp config               # show resolved config path
    rag-mcp config --init        # seed config from template and exit
    rag-mcp docs                 # list bundled docs
    rag-mcp docs cli              # print the full CLI reference
    rag-mcp docs <name>          # print a bundled doc to stdout
    rag-mcp get_metrics          # replay the JSONL event log as a readable report
    rag-mcp get_metrics --json   # same, raw JSON (same shape as the MCP tool)
    rag-mcp get_metrics --file P # replay a specific events.jsonl
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
    "observability": "OBSERVABILITY.md",
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


def _resolve_events_log_path(args: list[str]) -> Path:
    """Where the JSONL event log lives, highest priority first:
    --file PATH > RAG_OBSERVABILITY_LOG_PATH > <storage.path>/observability/events.jsonl
    (storage.path resolved exactly like the server does, without touching ChromaDB)."""
    import os
    from rag_mcp import observability

    if "--file" in args:
        idx = args.index("--file")
        if idx + 1 >= len(args):
            print("Usage: rag-mcp get_metrics [--json] [--file PATH]", file=sys.stderr)
            sys.exit(2)
        return Path(args[idx + 1])

    env = os.environ.get(observability.ENV_LOG_PATH, "").strip()
    if env:
        return Path(env)

    from rag_mcp.config_loader import ConfigLoader
    from rag_mcp.paths import resolve_config_path, resolve_data_path

    config = ConfigLoader(resolve_config_path()).load()
    storage_path = Path(config.storage.path)
    if not storage_path.is_absolute():
        storage_path = resolve_data_path()
    return observability.default_log_path(storage_path)


def _get_metrics_main(args: list[str]) -> None:
    import json
    from rag_mcp import observability

    snapshot = observability.aggregate_events_from_log(_resolve_events_log_path(args))
    if "--json" in args:
        print(json.dumps(snapshot, indent=2))
    else:
        print(observability.format_metrics_text(snapshot))


def main() -> None:
    """CLI dispatcher — routes subcommands to server or indexer logic."""
    from rag_mcp.paths import resolve_config_path, resolve_data_path

    args = sys.argv[1:]
    command = args[0] if args else "serve"

    match command:
        case "docs":
            _docs_main(args)

        case "get_metrics":
            _get_metrics_main(args[1:])

        case "config":
            cfg = resolve_config_path()
            data = resolve_data_path()
            print(f"Config : {cfg}")
            print(f"Data   : {data}")
            if "--init" in args:
                print("Config file seeded (or already exists).")

        case "index":
            _prepare_env()
            rest = args[1:] if args and args[0] == "index" else []
            sys.argv = [sys.argv[0]] + rest

            from rag_mcp._indexer import main as indexer_main
            indexer_main()

        case "serve":
            _prepare_env()
            rest = args[1:] if args and args[0] == "serve" else args
            sys.argv = [sys.argv[0]] + rest

            from rag_mcp._server import main as server_main
            server_main()

        case _:
            print(
                "Usage: rag-mcp <command> [options]\n"
                "\n"
                "Commands:\n"
                "  serve    Start the MCP server (default: stdio transport)\n"
                "  index    Index project files into ChromaDB\n"
                "  config   Show resolved config and data paths\n"
                "  docs     List or print bundled documentation (tools, log-indexing, log-patterns, cli, observability)\n"
                "  get_metrics  Replay the JSONL event log (tool calls, embed failures, backfills); --json for raw output\n"
                "\n"
                "Run 'rag-mcp <command> --help' for command-specific options.\n",
                file=sys.stderr,
            )
            sys.exit(1)

def _prepare_env() -> Path:
    """Resolve config/data paths, ensure config has data path set, and export RAG_CONFIG_PATH."""
    from rag_mcp.paths import resolve_config_path, resolve_data_path

    cfg_path = resolve_config_path()
    data_path = resolve_data_path()
    _ensure_data_path_in_config(cfg_path, data_path)

    import os
    os.environ.setdefault("RAG_CONFIG_PATH", str(cfg_path))

    return cfg_path

if __name__ == "__main__":
    main()
