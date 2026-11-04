"""Shared helper functions used across tool modules."""

import time
from typing import Any

from rag_mcp import observability


def log_tool_call(
    tool_name: str,
    params: dict,
    start_time: float,
    result_count: int,
    outcome: str = "ok",
    error: str | None = None,
):
    """Log a tool invocation as one structured ``tool_call`` JSON line.

    Written to stderr and appended to the JSONL events file (see
    ``rag_mcp.observability``). ``outcome`` is ``"ok"`` (default) or
    ``"error"``; on error pass the text returned to the client as ``error``.
    Positional signature unchanged so existing call sites are untouched.
    """
    observability.log_tool_call_event(tool_name, params, start_time, result_count, outcome, error)


class ToolCall:
    """One tool invocation: captures name, params and start time at entry.

    Every return path of a tool body goes through exactly one of::

        return call.ok(result_count, text)   # emits outcome="ok"
        return call.error(text)              # emits outcome="error", result_count=0

    Both return ``text`` unchanged, so the client sees exactly the same
    string as before and each invocation emits exactly one ``tool_call``.
    """

    __slots__ = ("tool", "params", "start_time")

    def __init__(self, tool: str, params: dict[str, Any]):
        self.tool = tool
        self.params = params
        self.start_time = time.time()

    def ok(self, result_count: int, text: str) -> str:
        log_tool_call(self.tool, self.params, self.start_time, result_count)
        return text

    def error(self, text: str) -> str:
        log_tool_call(self.tool, self.params, self.start_time, 0, outcome="error", error=text)
        return text


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
