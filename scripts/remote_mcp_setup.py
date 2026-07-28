# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Standalone MCP config writer for rag-mcp-new-pip-mcp — no repo checkout required.

Unlike scripts/setup_mcp_config.py (which reads config/server_info.json and
writes .vscode/mcp.json relative to the repo root), this script has ZERO
dependency on the pow-mcp-rag-new repo. It only uses the Python standard
library, so it can run on any machine via a single command:

    uv run remote_mcp_setup.py --index-url <PYPI_INDEX_URL>
    # or, directly from a URL (uv supports running remote scripts):
    uv run https://<host>/remote_mcp_setup.py --index-url <PYPI_INDEX_URL>
    # or with plain Python (no uv needed for this script itself):
    python remote_mcp_setup.py --index-url <PYPI_INDEX_URL>

It writes/updates the rag-mcp-new-pip-mcp entry in:
    - ~/.kiro/settings/mcp.json   (if --kiro / default, and ~/.kiro exists or --force-kiro)
    - <path>/.vscode/mcp.json     (only if --vscode-dir is given — no repo to
                                    default to, unlike setup_mcp_config.py)

Typical remote-machine flow (once a hosted index exists):
    1. uv tool install --extra-index-url <INDEX_URL> rag-mcp-new-pip-mcp
    2. uv run remote_mcp_setup.py --index-url <INDEX_URL> --stable
    3. Restart Kiro / VS Code

Usage:
    python remote_mcp_setup.py --index-url <URL> [options]

Options:
    --index-url URL       PyPI-compatible index URL (required)
    --package NAME        Package name (default: rag-mcp-new-pip-mcp)
    --server-name NAME    MCP server key in mcp.json (default: rag-mcp-new-pip-mcp)
    --stable               Point mcp.json at a persistent 'uv tool install' exe
                           (~/.local/bin/<package>[.exe]) instead of 'uvx --from'.
                           Avoids re-resolving the dependency tree on every launch
                           (and the associated Windows Defender trampoline-exe
                           race — see pow-mcp-rag-new's doc/TROUBLESHOOTING.md).
                           Requires having already run:
                             uv tool install --extra-index-url <URL> --force <package>
    --kiro-path PATH       Explicit mcp.json path (default: ~/.kiro/settings/mcp.json)
    --skip-kiro             Don't write the Kiro config at all
    --vscode-dir DIR       Write <DIR>/.vscode/mcp.json (omit to skip VS Code config —
                           there's no repo root to default to on a remote machine)
    --no-reindex-flag      Omit the "--no-reindex" server arg (default: included)
    --no-hf-offline        Omit HF_HUB_OFFLINE=1 from the entry's env (needed if the
                           embedding/reranker models aren't cached locally yet)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_SERVER_NAME = "rag-mcp-new-pip-mcp"
DEFAULT_PACKAGE = "rag-mcp-new-pip-mcp"
DEFAULT_AUTO_APPROVE = ["search_docs", "list_projects", "list_files", "get_document"]
# Suppresses sentence-transformers'/huggingface_hub's network reachability
# check on every startup once the embedding model and reranker are already
# cached locally (~/.cache/huggingface/hub/), which they are after the first
# successful run. Pass --no-hf-offline to omit this if you need the very
# first run to download an uncached model.
DEFAULT_MCP_ENV = {"HF_HUB_OFFLINE": "1"}


def _write_entry(mcp_path: Path, server_name: str, new_entry: dict, servers_key: str) -> None:
    """Merge a single server entry into mcp.json, preserving other servers."""
    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [warn] Existing {mcp_path.name} is malformed ({e}), backing up and recreating.", file=sys.stderr)
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

    verb = "Updated" if existing else "Added"
    print(f"  {verb} '{server_name}' entry in {mcp_path.name}")

    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"  Path: {mcp_path}")

    other_servers = [k for k in config[servers_key] if k != server_name]
    if other_servers:
        print(f"  Other servers preserved: {', '.join(other_servers)}")


def _stable_exe_path(package: str) -> str:
    """Resolve the console-script exe path a `uv tool install` would produce.

    uv places tool exes at ~/.local/bin on all platforms (adding .exe on
    Windows automatically via PATHEXT resolution — but we spell it out
    explicitly on Windows since MCP client 'command' fields don't always
    consult PATHEXT).
    """
    base = Path.home() / ".local" / "bin" / package
    if sys.platform == "win32":
        return str(base.with_suffix(".exe")).replace("\\", "/")
    return str(base)


def build_entry(args: argparse.Namespace) -> tuple[dict, dict]:
    """Build the (kiro_entry, vscode_entry) dicts for the requested mode."""
    server_args = ["serve"]
    if not args.no_reindex_flag:
        server_args.append("--no-reindex")

    if args.stable:
        command = _stable_exe_path(args.package)
        run_args = server_args
        print(
            "  [mode] --stable: pointing mcp.json at the persistent 'uv tool install' exe.\n"
            "         Make sure it's installed/up to date:\n"
            f"           uv tool install --extra-index-url {args.index_url} --force {args.package}"
        )
    else:
        command = "uvx"
        run_args = ["--extra-index-url", args.index_url, "--from", args.package, args.package] + server_args
        print("  [mode] uvx --from: re-resolves dependencies on every launch (see --stable for an alternative).")

    env = {} if args.no_hf_offline else dict(DEFAULT_MCP_ENV)

    kiro_entry = {
        "command": command,
        "args": run_args,
        "disabled": False,
        "autoApprove": DEFAULT_AUTO_APPROVE,
    }
    vscode_entry = {
        "type": "stdio",
        "command": command,
        "args": run_args,
    }
    if env:
        kiro_entry["env"] = env
        vscode_entry["env"] = env
    return kiro_entry, vscode_entry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write/update the rag-mcp-new-pip-mcp MCP server entry — no repo checkout required.",
    )
    parser.add_argument("--index-url", required=True, help="PyPI-compatible index URL (local pypiserver or hosted S3/CodeArtifact)")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help=f"Package name to install/run (default: {DEFAULT_PACKAGE})")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME, help=f"MCP server key in mcp.json (default: {DEFAULT_SERVER_NAME})")
    parser.add_argument("--stable", action="store_true", help="Use a persistent 'uv tool install' exe instead of 'uvx --from'")
    parser.add_argument("--kiro-path", default=None, help="Explicit mcp.json path (default: ~/.kiro/settings/mcp.json)")
    parser.add_argument("--skip-kiro", action="store_true", help="Don't write the Kiro config")
    parser.add_argument("--vscode-dir", default=None, help="Write <DIR>/.vscode/mcp.json (omitted by default — no repo root to infer)")
    parser.add_argument("--no-reindex-flag", action="store_true", help="Omit the --no-reindex server arg (default: included)")
    parser.add_argument("--no-hf-offline", action="store_true", help="Omit HF_HUB_OFFLINE=1 from the entry's env (needed if the embedding/reranker models aren't cached locally yet)")
    args = parser.parse_args()

    kiro_entry, vscode_entry = build_entry(args)

    wrote_anything = False

    if not args.skip_kiro:
        kiro_path = Path(args.kiro_path) if args.kiro_path else (Path.home() / ".kiro" / "settings" / "mcp.json")
        print(f"\nUpdating Kiro config...")
        _write_entry(kiro_path, args.server_name, kiro_entry, servers_key="mcpServers")
        wrote_anything = True
    else:
        print("\n[skip] --skip-kiro set, not touching Kiro config.")

    if args.vscode_dir:
        vscode_path = Path(args.vscode_dir) / ".vscode" / "mcp.json"
        print(f"\nUpdating VS Code config...")
        _write_entry(vscode_path, args.server_name, vscode_entry, servers_key="servers")
        wrote_anything = True
    else:
        print("[skip] --vscode-dir not given, not writing .vscode/mcp.json.")

    if not wrote_anything:
        print("\nNothing written (both --skip-kiro set and --vscode-dir omitted).", file=sys.stderr)
        sys.exit(1)

    print("\nDone. Restart Kiro / VS Code (or reconnect the MCP server) to apply.")


if __name__ == "__main__":
    main()
