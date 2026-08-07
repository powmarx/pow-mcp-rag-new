"""Operation registry for JSON casepacks."""

from __future__ import annotations

from typing import Callable

Operation = Callable[..., str]


def resolve_operation(operation_key: str) -> Operation:
    if operation_key not in _OPERATIONS:
        known = ", ".join(sorted(_OPERATIONS))
        raise KeyError(f"Unknown operation '{operation_key}'. Known operations: {known}")
    return _OPERATIONS[operation_key]()


_OPERATIONS: dict[str, Callable[[], Operation]] = {
    "management.remove_project": lambda: _import_management("remove_project"),
    "management.clear_project_index": lambda: _import_management("clear_project_index"),
    "management.add_file_sync": lambda: _import_management("_add_file_sync"),
    "management.add_folder_sync": lambda: _import_management("_add_folder_sync"),
    "management.add_project_sync": lambda: _import_management("_add_project_sync"),
    "search.search_docs_sync": lambda: _import_search("_search_docs_sync"),
    "search.search_code_sync": lambda: _import_search("_search_code_sync"),
    "search.find_function_sync": lambda: _import_search("_find_function_sync"),
    "search.search_hex_pattern": lambda: _import_search("search_hex_pattern"),
    "documents.get_project_summary": lambda: _import_documents("get_project_summary"),
    "documents.compare_projects_sync": lambda: _import_documents("_compare_projects_sync"),
}


def _import_management(name: str) -> Operation:
    from rag_mcp.tools import management

    return getattr(management, name)


def _import_search(name: str) -> Operation:
    from rag_mcp.tools import search

    return getattr(search, name)


def _import_documents(name: str) -> Operation:
    from rag_mcp.tools import documents

    return getattr(documents, name)

