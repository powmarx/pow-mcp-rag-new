"""
Installs or updates the project-rag MCP server entry in an mcp.json file.

Merges into existing config without overwriting other servers.
Called by setup.bat / setup.sh (native mode), setup-docker.* (docker mode),
and setup-pypi.* (pip/uvx mode, installing from a local or remote PyPI index).

Usage:
    # Native (local venv) mode:
    python setup_mcp_config.py <python_exe_path> <server_script_path>

    # Docker mode:
    python setup_mcp_config.py --docker \
        --projects-dir <HOST_PROJECTS_DIR> \
        --repo-dir <HOST_REPO_DIR> \
        [--image rag-mcp-new-pip:latest] \
        [--data-volume rag-mcp-new-pip-data] \
        [--out <mcp.json path>]

    # pip/uvx mode (installed from a local or remote PyPI index):
    python setup_mcp_config.py --uvx \
        --index-url <PYPI_INDEX_URL> \
        [--package rag-mcp-new-pip-mcp] \
        [--vscode]  # also write .vscode/mcp.json in this repo
        [--stable]  # point mcp.json at a persistent 'uv tool install' exe instead
                    # of 'uvx --from' (recommended on Windows — avoids an
                    # intermittent Defender/trampoline-exe race on every launch;
                    # see doc/TROUBLESHOOTING.md). Run
                    # 'uv tool install --extra-index-url <url> --force <package>'
                    # once before using this flag.
        [--out <mcp.json path>]
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_AUTO_APPROVE = ["search_docs", "list_projects", "list_files", "get_document"]
VSCODE_AUTO_APPROVE = DEFAULT_AUTO_APPROVE
# Suppresses sentence-transformers'/huggingface_hub's network reachability
# check on every startup ("Warning: You are sending unauthenticated requests
# to the HF Hub...") once the embedding model and reranker are already
# cached locally (~/.cache/huggingface/hub/), which they are after the first
# successful run. See doc/PIP_INSTALL_GUIDE.md's MCP config section.
DEFAULT_MCP_ENV = {"HF_HUB_OFFLINE": "1"}


def _server_name(override: str | None = None) -> str:
    if override:
        return override
    script_dir = Path(__file__).parent.parent  # scripts/ -> project root
    info_path = script_dir / "config" / "server_info.json"
    with open(info_path, "r", encoding="utf-8") as f:
        return json.load(f)["name"]


def _write_entry(mcp_path: Path, server_name: str, new_entry: dict, servers_key: str = "mcpServers"):
    """Merge a single server entry into mcp.json, preserving other servers.

    servers_key is "mcpServers" for Kiro's ~/.kiro/settings/mcp.json and
    "servers" for VS Code / VS 2026's .vscode/mcp.json (different schema key,
    and VS Code entries use "type": "stdio" instead of "disabled"/"autoApprove").
    """
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [warn] Existing mcp.json is malformed ({e}), backing up and recreating.", file=sys.stderr)
            backup = mcp_path.with_suffix(".json.bak")
            mcp_path.rename(backup)
            print(f"  Backup saved to: {backup}", file=sys.stderr)
            config = {}
    else:
        config = {}

    if servers_key not in config:
        config[servers_key] = {}

    existing = config[servers_key].get(server_name)
    config[servers_key][server_name] = new_entry

    if existing:
        print(f"  Updated '{server_name}' entry in existing {mcp_path.name}")
    else:
        print(f"  Added '{server_name}' entry to {mcp_path.name}")

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"  Path: {mcp_path}")

    other_servers = [k for k in config[servers_key] if k != server_name]
    if other_servers:
        print(f"  Other servers preserved: {', '.join(other_servers)}")


def _docker_main():
    parser = argparse.ArgumentParser(prog="setup_mcp_config.py --docker")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--server-name", default=None, help="MCP server key in mcp.json (default: from server_info.json)")
    parser.add_argument("--projects-dir", required=True, help="Host dir holding source repos (mounted at /projects)")
    parser.add_argument("--image", default="rag-mcp-new-pip:latest")
    parser.add_argument("--data-volume", default="rag-mcp-new-pip-data")
    parser.add_argument("--out", default=None, help="mcp.json path (default: ~/.kiro/settings/mcp.json)")
    args = parser.parse_args()

    projects_dir = args.projects_dir.replace("\\", "/")
    mcp_path = Path(args.out) if args.out else (Path.home() / ".kiro" / "settings" / "mcp.json")

    # Config lives in the data volume (RAG_CONFIG_PATH baked into the image), so
    # the entry only needs the source mount, the data volume, and the image —
    # no dependency on the pow-mcp-rag-new repo path.
    # --no-reindex is important: the DB lives in a shared volume, so if the
    # server auto-reindexed on startup, every Kiro connection would spawn a
    # concurrent writer and corrupt ChromaDB. Indexing is a separate step
    # (setup-docker / `docker compose run indexer`).
    new_entry = {
        "command": "docker",
        "args": [
            "run", "-i", "--rm",
            "-v", f"{projects_dir}:/projects:ro",
            "-v", f"{args.data_volume}:/app/data",
            args.image,
            "python", "server.py", "--no-reindex",
        ],
        "disabled": False,
        "autoApprove": DEFAULT_AUTO_APPROVE,
    }
    _write_entry(mcp_path, _server_name(args.server_name), new_entry)


def _uvx_main():
    parser = argparse.ArgumentParser(prog="setup_mcp_config.py --uvx")
    parser.add_argument("--uvx", action="store_true")
    parser.add_argument("--server-name", default=None, help="MCP server key in mcp.json (default: from server_info.json)")
    parser.add_argument("--index-url", required=True, help="PyPI-compatible index URL (local pypiserver or S3-hosted index)")
    parser.add_argument("--package", default="rag-mcp-new-pip-mcp", help="Package name to install/run via uvx")
    parser.add_argument("--out", default=None, help="mcp.json path (default: ~/.kiro/settings/mcp.json)")
    parser.add_argument("--vscode", action="store_true", help="Also write .vscode/mcp.json in this repo (VS Code / VS 2026)")
    parser.add_argument(
        "--stable", action="store_true",
        help="Point mcp.json at a persistent 'uv tool install'-ed exe (~/.local/bin/<package>.exe) "
             "instead of re-resolving via 'uvx --from' on every launch. Avoids the intermittent "
             "Windows Defender trampoline-exe race with large dependency trees (see TROUBLESHOOTING.md). "
             "Run 'uv tool install --extra-index-url <url> --force <package>' once (or after each rebuild).",
    )
    args = parser.parse_args()

    mcp_path = Path(args.out) if args.out else (Path.home() / ".kiro" / "settings" / "mcp.json")
    server_name = _server_name(args.server_name)

    # `uvx --from ... <pkg> ...` resolves and caches the ~110-package
    # dependency tree on EVERY launch (uv still has to re-check the cache and
    # regenerate a trampoline .exe for the entry point). On Windows this races
    # Defender's real-time scanner on the trampoline .exe write and fails
    # intermittently ("Failed to update Windows PE resources... Acesso
    # negado") — see doc/TROUBLESHOOTING.md. `uv tool install` instead
    # resolves ONCE into a persistent isolated environment and drops a stable
    # console-script exe at ~/.local/bin/<package>.exe (same pattern as a
    # plain global pip install, e.g. this repo's own graphify MCP entries) —
    # no re-resolution, no trampoline regeneration, no race on every launch.
    if args.stable:
        exe_path = str(Path.home() / ".local" / "bin" / f"{args.package}.exe").replace("\\", "/")
        command = exe_path
        run_args = ["serve", "--no-reindex"]
        print(
            f"  [uvx] --stable mode: pointing mcp.json at the persistent tool exe.\n"
            f"        Run this once (or after each rebuild) to install/update it:\n"
            f"          uv tool install --extra-index-url {args.index_url} --force {args.package}",
        )
    else:
        command = "uvx"
        run_args = [
            "--extra-index-url", args.index_url,
            "--from", args.package,
            args.package,
            "serve", "--no-reindex",
        ]

    kiro_entry = {
        "command": command,
        "args": run_args,
        "env": dict(DEFAULT_MCP_ENV),
        "disabled": False,
        "autoApprove": DEFAULT_AUTO_APPROVE,
    }
    _write_entry(mcp_path, server_name, kiro_entry)

    if args.vscode:
        vscode_path = Path(__file__).parent.parent / ".vscode" / "mcp.json"
        vscode_entry = {
            "type": "stdio",
            "command": command,
            "args": run_args,
            "env": dict(DEFAULT_MCP_ENV),
        }
        _write_entry(vscode_path, server_name, vscode_entry, servers_key="servers")


def main():
    if "--docker" in sys.argv:
        _docker_main()
        return

    if "--uvx" in sys.argv:
        _uvx_main()
        return

    if len(sys.argv) < 3:
        print("Usage: setup_mcp_config.py <python_exe> <server_script> [--server-name NAME]", file=sys.stderr)
        sys.exit(1)

    python_exe = sys.argv[1].replace("\\", "/")
    server_script = sys.argv[2].replace("\\", "/")

    # Optional --server-name override
    name_override = None
    if "--server-name" in sys.argv:
        idx = sys.argv.index("--server-name")
        if idx + 1 < len(sys.argv):
            name_override = sys.argv[idx + 1]
    server_name = _server_name(name_override)

    mcp_path = Path.home() / ".kiro" / "settings" / "mcp.json"

    # New server entry
    new_entry = {
        "command": python_exe,
        "args": [server_script],
        "disabled": False,
        "autoApprove": DEFAULT_AUTO_APPROVE,
    }

    _write_entry(mcp_path, server_name, new_entry)


if __name__ == "__main__":
    main()
