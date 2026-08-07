"""JSON-driven validation tests for MCP tools."""

from __future__ import annotations

import pytest

from tests.runners.casepacks import load_cases
from tests.runners.dispatch import run_case


CASES = load_cases("mcp_tools.validation.json")


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_mcp_validation_casepack(case, isolated_server_context):
    run_case(case)

