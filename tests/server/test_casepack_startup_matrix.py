"""JSON-driven selected startup matrix tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.runners.casepacks import load_cases
from tests.runners.dispatch import run_case


REPO_ROOT = Path(__file__).parent.parent.parent
CASES = load_cases("server.startup.json")


@pytest.fixture()
def startup_case_context(server_subprocess_env: dict[str, str]) -> dict[str, str]:
    return {
        "python": sys.executable,
        "server_script": str(REPO_ROOT / "server.py"),
        "rag_config_path": server_subprocess_env["RAG_CONFIG_PATH"],
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_server_startup_casepack(case, startup_case_context):
    run_case(case, context={"fixture": startup_case_context})

