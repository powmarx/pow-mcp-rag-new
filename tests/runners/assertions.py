"""Assertion helpers for JSON-driven test cases."""

from __future__ import annotations

import re
from typing import Any


def assert_expected_text(result: str, expect: dict[str, Any]) -> None:
    contains = expect.get("contains", [])
    for token in contains:
        assert token in result, f"Expected '{token}' in result:\n{result}"

    not_contains = expect.get("not_contains", [])
    for token in not_contains:
        assert token not in result, f"Expected '{token}' to be absent:\n{result}"

    contains_any = expect.get("contains_any", [])
    if contains_any:
        assert any(token in result for token in contains_any), (
            f"Expected at least one of {contains_any} in result:\n{result}"
        )

    equals = expect.get("equals")
    if equals is not None:
        assert result == equals, f"Expected exact result:\n{equals}\nActual:\n{result}"

    starts_with = expect.get("starts_with")
    if starts_with is not None:
        assert result.startswith(starts_with), (
            f"Expected result to start with '{starts_with}':\n{result}"
        )

    not_starts_with = expect.get("not_starts_with")
    if not_starts_with is not None:
        assert not result.startswith(not_starts_with), (
            f"Expected result not to start with '{not_starts_with}':\n{result}"
        )

    regex = expect.get("regex")
    if regex is not None:
        assert re.search(regex, result), f"Expected regex '{regex}' to match:\n{result}"

    length_equals = expect.get("length_equals")
    if length_equals is not None:
        assert len(result) == length_equals, (
            f"Expected result length {length_equals}, got {len(result)}"
        )

    length_gte = expect.get("length_gte")
    if length_gte is not None:
        assert len(result) >= length_gte, f"Expected result length >= {length_gte}, got {len(result)}"

    length_lte = expect.get("length_lte")
    if length_lte is not None:
        assert len(result) <= length_lte, f"Expected result length <= {length_lte}, got {len(result)}"
