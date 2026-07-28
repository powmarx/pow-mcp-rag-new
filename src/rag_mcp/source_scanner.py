"""
Shared source-pattern scanner.

Given a project root folder and the app config, produce the list of recursive
source patterns to index — one ``**/*.ext`` per configured file type that
actually exists under the folder. Used by both the discovery script
(setup_discover.py) and the add_project MCP tool so they behave identically.
"""

import os
from pathlib import Path

from rag_mcp.config_loader import AppConfig, SourcePattern


def build_source_patterns(root: Path, config: AppConfig) -> list[SourcePattern]:
    """
    Walk ``root`` and return recursive source patterns for the configured file
    types (config.index_extensions) that exist under it. Directories in
    config.excluded_dirs are pruned. When PDFs are present, the ``.md`` pattern
    is always included (PDFs are converted to Markdown at index time).
    """
    excluded_dirs = set(config.excluded_dirs)
    ext_meta = {ie.ext: (ie.type, ie.description) for ie in config.index_extensions}
    wanted = set(ext_meta)

    present: set[str] = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in wanted:
                present.add(ext)
        if len(present) == len(wanted):
            break  # found everything we care about; stop walking

    # PDFs are converted to .md and indexed via the .md pattern, so ensure it's
    # present whenever PDFs exist.
    if ".pdf" in present and ".md" in wanted:
        present.add(".md")

    sources: list[SourcePattern] = []
    for ie in config.index_extensions:  # preserve configured order
        if ie.ext in present:
            sources.append(SourcePattern(pattern=f"**/*{ie.ext}", type=ie.type, description=ie.description))
    return sources
