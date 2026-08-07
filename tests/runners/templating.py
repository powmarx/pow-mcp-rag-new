"""Template substitution for fixture-aware casepacks."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


_FIXTURE_TOKEN = re.compile(r"\{\{\s*fixture\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def resolve_case(case: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not context:
        return case
    resolved = deepcopy(case)
    return _resolve_value(resolved, context)


def _resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, context) for v in value]
    if isinstance(value, str):
        return _resolve_string(value, context)
    return value


def _resolve_string(value: str, context: dict[str, Any]) -> str:
    fixture_values = context.get("fixture", {})

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in fixture_values:
            raise KeyError(f"Missing fixture context key '{key}' for case template '{value}'")
        return str(fixture_values[key])

    return _FIXTURE_TOKEN.sub(replace, value)

