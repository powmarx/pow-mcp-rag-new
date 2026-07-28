"""Shared helper functions used across tool modules."""

import sys
import time


def log_tool_call(tool_name: str, params: dict, start_time: float, result_count: int):
    """Log tool invocation to stderr."""
    elapsed_ms = (time.time() - start_time) * 1000
    query = params.get("query", "")
    if len(query) > 100:
        query = query[:100] + "..."
    params_str = ", ".join(f"{k}={repr(v)}" for k, v in params.items() if k != "query")
    if query:
        params_str = f"query={repr(query)}" + (f", {params_str}" if params_str else "")
    print(
        f"[tool] {tool_name}({params_str}) -> {result_count} results in {elapsed_ms:.0f}ms",
        file=sys.stderr,
    )


def validate_path(file_path: str) -> str | None:
    """Validate file_path is relative and has no traversal. Returns error message or None."""
    if not file_path:
        return "file_path is required"
    if ".." in file_path:
        return "file_path must not contain '..'"
    if file_path.startswith("/") or file_path.startswith("\\"):
        return "file_path must be relative (not absolute)"
    if len(file_path) >= 2 and file_path[1] == ":":
        return "file_path must be relative (not absolute)"
    return None


def extract_snippet(text: str, search_term: str, context_chars: int = 200) -> str:
    """Extract a snippet around the first occurrence of search_term in text."""
    lower_text = text.lower()
    lower_term = search_term.lower()
    pos = lower_text.find(lower_term)

    if pos == -1:
        pos = text.find(search_term)
        if pos == -1:
            return text[:400] if len(text) > 400 else text

    start = max(0, pos - context_chars)
    end = min(len(text), pos + len(search_term) + context_chars)

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."

    return snippet


# Extension-to-type mapping (shared by add_file and add_folder)
FILE_TYPE_MAP = {
    ".h": "header", ".hpp": "header",
    ".c": "source", ".cpp": "source", ".cc": "source",
    ".py": "source", ".go": "source", ".cs": "source",
    ".ts": "source", ".js": "source",
    ".md": "documentation", ".txt": "documentation", ".pdf": "documentation",
    ".ini": "config", ".json": "config", ".yaml": "config", ".yml": "config",
}
