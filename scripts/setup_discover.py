"""
Auto-discovery script for setup.bat / setup-docker.*

Lists ALL root folders under PROJECTS_ROOT, shows them to the user, and asks
which ones to index. Each selected root folder is added to config.yaml as a
single project (nested code included). Source patterns are auto-detected.

Selection:
  - Interactive (a TTY): the user picks from a numbered list.
  - Non-interactive (no TTY): all listed folders are selected (a note is printed).
  - Scripted: pass --select "1,3,5" / --select all / --select none, or --all.
"""

import shutil
import sys
from pathlib import Path

# Add src/ to path (this script lives in scripts/, so go up to the project root)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from rag_mcp.config_loader import ConfigLoader, ProjectConfig
from rag_mcp.source_scanner import build_source_patterns


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Discover and select root folders to index")
    parser.add_argument("projects_root", type=str, help="Parent directory to scan")
    parser.add_argument("--ignore", nargs="*", default=[],
                        help="Prefix patterns to hide from the list (e.g., tools- components-)")
    parser.add_argument("--select", default=None,
                        help="Non-interactive selection: comma-separated numbers, 'all', or 'none'")
    parser.add_argument("--all", action="store_true", help="Select all discovered folders")
    parser.add_argument("--list", action="store_true", dest="list_only",
                        help="Only print the numbered root-folder list and exit (no selection/write)")
    parser.add_argument(
        "--config",
        default=None,
        help="Config file to read/write (default: <root>/config/config.yaml). "
             "Created from config.template.yaml if it doesn't exist.",
    )
    args = parser.parse_args()

    projects_root = Path(args.projects_root).resolve()
    if not projects_root.exists():
        print(f"Error: PROJECTS_ROOT does not exist: {projects_root}", file=sys.stderr)
        sys.exit(1)

    script_dir = Path(__file__).parent.parent  # scripts/ -> project root
    config_path = Path(args.config) if args.config else (script_dir / "config" / "config.yaml")
    this_repo_name = script_dir.name
    template_path = script_dir / "config" / "config.template.yaml"

    # Seed the target config from the template if it doesn't exist yet
    # (skipped in --list mode: merely listing folders shouldn't create files).
    if not args.list_only and not config_path.exists() and template_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template_path, config_path)
        print(f"  Created {config_path.name} from template")

    config = None
    loader = None
    if config_path.exists():
        loader = ConfigLoader(config_path)
        try:
            config = loader.load()
        except Exception as e:
            print(f"Error loading config: {e}", file=sys.stderr)
            sys.exit(1)

    # discovery_ignore ALWAYS applies (from config). If the target config doesn't
    # exist yet — e.g. the --list step that runs before --select seeds it — read
    # the prefixes from the template so --list and --select stay consistent.
    ignore_source = config
    if ignore_source is None and template_path.exists():
        try:
            ignore_source = ConfigLoader(template_path).load()
        except Exception:
            ignore_source = None
    base_ignore = list(ignore_source.discovery_ignore) if ignore_source else []
    ignore_prefixes = list(dict.fromkeys(base_ignore + list(args.ignore)))

    # ---- Discover ALL root folders --------------------------------------
    root_dirs = []
    for item in sorted(projects_root.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith("."):
            continue
        if ignore_prefixes and any(item.name.startswith(p) for p in ignore_prefixes):
            continue
        root_dirs.append(item)

    if not root_dirs:
        print(f"No root folders found under {projects_root}")
        return

    # Print the numbered list for --list and for the interactive prompt, but
    # skip it on scripted --select/--all runs (the caller already listed it).
    if args.list_only or (args.select is None and not args.all):
        print(f"\nRoot folders under {projects_root}:\n")
        for i, d in enumerate(root_dirs, 1):
            marker = "git" if (d / ".git").exists() else "no-git"
            print(f"  [{i}] {d.name}  ({marker})")
        print()

    # --list: print the numbered list and stop (host-side selection flows use
    # this, then re-invoke with --select).
    if args.list_only:
        return

    # ---- Resolve selection ----------------------------------------------
    selection = _resolve_selection(root_dirs, args)
    if not selection:
        print("No folders selected. Nothing to do.")
        return

    # Keep curated (non auto-discovered) projects; drop prior auto-discovered
    # entries so the config reflects the current selection.
    kept = [p for p in config.projects if not p.description.startswith("Auto-discovered")]
    kept_paths = {_safe_resolve(p.base_path) for p in kept}
    config.projects = kept

    added = 0
    for d in selection:
        if _safe_resolve(str(d)) in kept_paths:
            print(f"  [skip] {d.name} (already configured as a curated project)")
            continue
        sources = build_source_patterns(d, config)
        if not sources:
            print(f"  [skip] {d.name} (no code or doc files found)")
            continue
        config.projects.append(ProjectConfig(
            name=d.name,
            description=f"Auto-discovered: {d.name}",
            base_path=str(d).replace("\\", "/"),
            sources=sources,
        ))
        print(f"  [added] {d.name} ({len(sources)} patterns)")
        added += 1

    loader.save(config)
    print(f"\nSaved {added} selected project(s) to {config_path.name}")


def _resolve_selection(root_dirs, args):
    """Return the subset of root_dirs to index based on flags / prompt / TTY."""
    n = len(root_dirs)

    def parse_choice(choice):
        choice = choice.strip().lower()
        if choice in ("", "none"):
            return []
        if choice == "all":
            return list(root_dirs)
        selected = []
        for tok in choice.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if not tok.isdigit() or not (1 <= int(tok) <= n):
                print(f"  Invalid selection: '{tok}' (must be 1-{n}, 'all', or 'none')")
                return None
            selected.append(root_dirs[int(tok) - 1])
        return selected

    if args.all:
        return list(root_dirs)
    if args.select is not None:
        result = parse_choice(args.select)
        return result or []

    if not sys.stdin.isatty():
        print("[non-interactive] No TTY detected — selecting ALL folders.")
        print("                  Use --select \"1,3\" or --all to choose explicitly.")
        return list(root_dirs)

    print("Select folders to index (comma-separated numbers, 'all', or 'none'):")
    while True:
        try:
            result = parse_choice(input("> "))
        except (EOFError, KeyboardInterrupt):
            print("\nSelection cancelled.")
            return []
        if result is not None:
            return result


def _safe_resolve(p) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(p)


if __name__ == "__main__":
    main()
