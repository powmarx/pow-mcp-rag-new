"""Dispatch a JSON case to its runner implementation."""

from __future__ import annotations

from typing import Any

from tests.runners.state_runner import run_state_case
from tests.runners.subprocess_runner import run_subprocess_case
from tests.runners.validation_runner import run_validation_case


def run_case(case: dict[str, Any], context: dict[str, Any] | None = None) -> None:
    runner = case["runner"]
    if runner == "validation_runner":
        run_validation_case(case, context=context)
        return
    if runner == "state_runner":
        run_state_case(case, context=context)
        return
    if runner == "subprocess_runner":
        run_subprocess_case(case, context=context)
        return
    raise AssertionError(f"Unsupported runner '{runner}'")
