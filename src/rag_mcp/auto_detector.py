"""
Project structure auto-detection using data-driven rules.

Reads detection_rules.json and applies heuristics to generate
config.yaml source entries for a given project directory.
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DetectedSource:
    """A detected source pattern to add to config.yaml."""

    pattern: str
    type: str
    description: str


def _resolve_default_rules_path() -> Path:
    """
    Locate detection_rules.json: prefer bundled package data (installed
    wheel/sdist via pip/uvx), fall back to the repo's config/ folder
    (editable install / repo checkout). Mirrors the resolution used for
    server_info.json in _server.py.
    """
    here = Path(__file__).parent
    candidates = [
        here / "data" / "detection_rules.json",
        here.parent.parent.parent / "config" / "detection_rules.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Neither found — return the packaged-data path so the missing-file
    # error message points somewhere meaningful instead of silently
    # falling back to empty rules.
    return candidates[0]

_DEFAULT_RULES_PATH = _resolve_default_rules_path()


class ProjectAutoDetector:
    """Scans a project directory and generates config.yaml source entries using data-driven rules."""

    def __init__(self, rules_path: Path | None = None):
        self.rules_path = rules_path or _DEFAULT_RULES_PATH
        self.rules = self._load_rules()

   
    def _load_rules(self) -> dict:
        """Load detection rules from JSON file."""
        if not self.rules_path.exists():
            import sys
            print(
                f"[auto_detector] WARNING: detection_rules.json not found at "
                f"{self.rules_path} — auto-detect will find nothing.",
                file=sys.stderr,
            )
            return {"common": {}, "stacks": {}}
        with open(self.rules_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def detect(self, project_path: Path) -> list[DetectedSource]:
        """
        Scan project_path and return detected source patterns.
        Applies stack-specific rules first, then common rules.
        """
        sources: list[DetectedSource] = []

        # Detect which stacks are present
        detected_stacks = self._detect_stacks(project_path)

        # Apply stack-specific rules
        for stack_name in detected_stacks:
            stack_rules = self.rules.get("stacks", {}).get(stack_name, {})
            sources.extend(self._apply_stack_rules(project_path, stack_rules))

        # Apply common rules
        common_rules = self.rules.get("common", {})
        sources.extend(self._apply_common_rules(project_path, common_rules))

        # Also check for submodules if no component/ dir was detected
        if not any(s.pattern.startswith("component") for s in sources):
            sources.extend(self._detect_submodules(project_path))

        return sources

    def _detect_stacks(self, path: Path) -> list[str]:
        """Determine which tech stacks are present based on markers."""
        detected = []
        for stack_name, stack_config in self.rules.get("stacks", {}).items():
            markers = stack_config.get("markers", [])
            marker_dirs = stack_config.get("marker_dirs", [])
            found = False

            # Check file markers
            for marker in markers:
                if "*" in marker:
                    if self._has_files(path, marker):
                        found = True
                        break
                elif (path / marker).exists():
                    found = True
                    break

            # Check directory markers if no file marker matched
            if not found:
                for marker_dir in marker_dirs:
                    if (path / marker_dir).is_dir():
                        found = True
                        break

            if found:
                detected.append(stack_name)

        return detected

    def _apply_stack_rules(self, path: Path, stack_config: dict) -> list[DetectedSource]:
        """Apply rules for a specific tech stack."""
        sources = []

        for rule in stack_config.get("rules", []):
            if not self._rule_matches(path, rule):
                continue

            # Handle direct vs nested file check
            if "has_direct_files" in rule:
                check_dir = rule.get("check_dir", [None])[0]
                dir_path = path / check_dir if check_dir else path
                direct_patterns = rule.get("has_direct_files", [])
                has_direct = any(self._has_files(dir_path, p) for p in direct_patterns)

                if has_direct:
                    patterns = rule.get("patterns_if_direct", [])
                else:
                    patterns = rule.get("patterns_if_nested", [])
            else:
                patterns = rule.get("patterns", [])

            for p in patterns:
                sources.append(DetectedSource(
                    pattern=p["glob"],
                    type=p["type"],
                    description=p["description"],
                ))

        # Apply config_patterns if any files exist
        for p in stack_config.get("config_patterns", []):
            if self._has_files_recursive(path, p["glob"]):
                sources.append(DetectedSource(
                    pattern=p["glob"],
                    type=p["type"],
                    description=p["description"],
                ))

        return sources

    def _apply_common_rules(self, path: Path, common_config: dict) -> list[DetectedSource]:
        """Apply common rules (documentation, README, etc.)."""
        sources = []

        for rule_group in common_config.get("documentation", []):
            # Check if directory exists (supports multiple names like doc/docs)
            check_dirs = rule_group.get("check_dir", [])
            check_files = rule_group.get("check_file", [])

            if check_dirs:
                found_dir = None
                for dir_name in check_dirs:
                    if (path / dir_name).is_dir():
                        found_dir = dir_name
                        break

                if found_dir is None:
                    continue

                # Add patterns with the found directory name
                for p in rule_group.get("patterns", []):
                    full_glob = f"{found_dir}/{p['glob']}"
                    # Only add if files actually exist
                    if self._has_files_recursive(path / found_dir, p["glob"]):
                        sources.append(DetectedSource(
                            pattern=full_glob,
                            type=p["type"],
                            description=p["description"],
                        ))

            elif check_files:
                for file_name in check_files:
                    if (path / file_name).exists():
                        for p in rule_group.get("patterns", []):
                            sources.append(DetectedSource(
                                pattern=p["glob"],
                                type=p["type"],
                                description=p["description"],
                            ))
                        break

        return sources

    def _rule_matches(self, path: Path, rule: dict) -> bool:
        """Check if a rule's conditions are met."""
        # Always-true rules
        if rule.get("always"):
            return True

        has_any_condition = False

        # Check directory existence
        check_dirs = rule.get("check_dir", [])
        if check_dirs:
            has_any_condition = True
            if not any((path / d).is_dir() for d in check_dirs):
                return False

        # Check file existence
        check_files = rule.get("check_file", [])
        if check_files:
            has_any_condition = True
            if not any((path / f).exists() for f in check_files):
                return False

        # Check if files matching a pattern exist (non-recursive)
        has_files = rule.get("has_files", [])
        if has_files:
            has_any_condition = True
            check_dir = rule.get("check_dir", [None])[0]
            search_path = path / check_dir if check_dir else path
            if not any(self._has_files(search_path, p) for p in has_files):
                return False

        # Check recursive file patterns
        has_files_recursive = rule.get("has_files_recursive", [])
        if has_files_recursive:
            has_any_condition = True
            if not any(self._has_files_recursive(path, p) for p in has_files_recursive):
                return False

        # has_direct_files is handled in _apply_stack_rules, just treat check_dir as the condition
        if "has_direct_files" in rule:
            has_any_condition = True

        # If no conditions were present at all, don't match
        if not has_any_condition:
            return False

        return True

    def _detect_submodules(self, path: Path) -> list[DetectedSource]:
        """Detect submodules from .gitmodules when no component/ dir exists."""
        sources = []
        gitmodules = path / ".gitmodules"
        if not gitmodules.exists():
            return sources

        submodule_dirs = self._parse_gitmodules(path)
        for sm_dir in submodule_dirs:
            sm_path = path / sm_dir
            if not sm_path.is_dir():
                continue
            if self._has_files_recursive(sm_path, "*.h"):
                sources.append(DetectedSource(f"{sm_dir}/**/*.h", "header", f"Submodule {sm_dir} headers"))
            if self._has_files_recursive(sm_path, "*.c"):
                sources.append(DetectedSource(f"{sm_dir}/**/*.c", "source", f"Submodule {sm_dir} C sources"))
            if self._has_files_recursive(sm_path, "*.cpp"):
                sources.append(DetectedSource(f"{sm_dir}/**/*.cpp", "source", f"Submodule {sm_dir} C++ sources"))

        return sources

    def _has_files(self, path: Path, pattern: str) -> bool:
        """Check if any files match a glob pattern (non-recursive)."""
        return any(path.glob(pattern))

    def _has_files_recursive(self, path: Path, pattern: str) -> bool:
        """Check if any files match a recursive glob pattern."""
        excluded = {"node_modules", "venv", ".venv", "build", "dist", "__pycache__", ".git"}
        clean_pattern = pattern.replace("**/", "")
        for item in path.rglob(clean_pattern):
            if not any(part in excluded for part in item.parts):
                return True
        return False

    def _parse_gitmodules(self, path: Path) -> list[str]:
        """Parse .gitmodules file and return list of submodule paths."""
        gitmodules = path / ".gitmodules"
        if not gitmodules.exists():
            return []

        paths = []
        try:
            with open(gitmodules, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("path"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            paths.append(parts[1].strip())
        except (OSError, IOError):
            pass

        return paths
