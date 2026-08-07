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

