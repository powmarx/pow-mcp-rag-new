"""Test-wide hooks and fixtures."""

from __future__ import annotations

import pytest

from tests.runners.casepacks import validate_all_casepacks


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail early if any JSON case packs are malformed."""
    errors = validate_all_casepacks()
    if errors:
        details = "\n".join(f"- {error.format()}" for error in errors)
        raise pytest.UsageError(f"Invalid JSON case packs detected:\n{details}")


_TOOL_MODULES = ("search", "documents", "management", "logs", "metrics")


@pytest.fixture(autouse=True)
def _restore_tool_ctx():
    """Undo any test's changes to the module-level ``_ctx`` in rag_mcp.tools.*

    ``register_all_tools()`` and direct ``mod._ctx = ...`` assignments replace
    shared singletons that tests in other packages rely on.
    """
    import importlib

    mods = [importlib.import_module(f"rag_mcp.tools.{n}") for n in _TOOL_MODULES]
    saved = {m: getattr(m, "_ctx", None) for m in mods}
    yield
    for m, ctx in saved.items():
        m._ctx = ctx
