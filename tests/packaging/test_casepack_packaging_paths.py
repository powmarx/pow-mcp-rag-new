"""JSON-driven packaging path/environment tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.runners.casepacks import load_cases
from tests.runners.dispatch import run_case


REPO_ROOT = Path(__file__).parent.parent.parent
CASES = load_cases("packaging.paths.json")


@pytest.fixture()
def packaging_case_context(tmp_path: Path) -> dict[str, str]:
    xdg_config_home = tmp_path / "xdg-config"
    xdg_data_home = tmp_path / "xdg-data"
    custom_config_path = tmp_path / "custom-config.yaml"
    custom_data_path = tmp_path / "custom-data"

    xdg_config_home.mkdir(parents=True, exist_ok=True)
    xdg_data_home.mkdir(parents=True, exist_ok=True)
    custom_data_path.mkdir(parents=True, exist_ok=True)

    custom_config_path.write_text("projects: []\n", encoding="utf-8")
    fallback_cfg = xdg_config_home / "rag-mcp" / "config.yaml"
    fallback_cfg.parent.mkdir(parents=True, exist_ok=True)
    fallback_cfg.write_text("projects: []\n", encoding="utf-8")

    return {
        "python": sys.executable,
        "src_path": str(REPO_ROOT / "src"),
        "xdg_config_home": str(xdg_config_home),
        "xdg_data_home": str(xdg_data_home),
        "custom_config_path": str(custom_config_path),
        "custom_data_path": str(custom_data_path),
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_packaging_path_casepack(case, packaging_case_context):
    run_case(case, context={"fixture": packaging_case_context})

