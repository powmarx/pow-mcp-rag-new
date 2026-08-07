"""Runner for stateful JSON cases."""

from __future__ import annotations

from typing import Any

from tests.runners.assertions import assert_expected_text
from tests.runners.operation_registry import resolve_operation
from tests.runners.templating import resolve_case


def run_state_case(
    case: dict[str, Any],
    context: dict[str, Any] | None = None,
    state_setup: Any | None = None,
    state_assert: Any | None = None,
) -> None:
    rendered_case = resolve_case(case, context)

    if state_setup is not None:
        state_setup(rendered_case)

    operation = resolve_operation(rendered_case["operation"])
    result = operation(**rendered_case.get("inputs", {}))
    assert isinstance(result, str), f"Operation result must be string, got {type(result).__name__}"
    assert_expected_text(result, rendered_case.get("expect", {}))

    if state_assert is not None:
        state_assert(rendered_case, result)
