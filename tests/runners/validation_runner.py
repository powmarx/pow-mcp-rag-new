"""Runner for validation-oriented JSON cases."""

from __future__ import annotations

from typing import Any

from tests.runners.assertions import assert_expected_text
from tests.runners.operation_registry import resolve_operation
from tests.runners.templating import resolve_case


def run_validation_case(case: dict[str, Any], context: dict[str, Any] | None = None) -> None:
    rendered_case = resolve_case(case, context)
    operation_key = rendered_case["operation"]
    inputs = rendered_case.get("inputs", {})
    expect = rendered_case.get("expect", {})

    operation = resolve_operation(operation_key)
    result = operation(**inputs)
    assert isinstance(result, str), (
        f"Operation '{operation_key}' must return string, got {type(result).__name__}"
    )
    assert_expected_text(result, expect)
