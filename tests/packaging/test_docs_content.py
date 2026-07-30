"""
Tests for documentation content validation.

Covers:
1. README.md contains required strings and version info (Requirement 9.1)
2. PIP_INSTALL_GUIDE.md has distinct headings for public and local flows (Requirement 9.2)
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
assert (REPO_ROOT / "pyproject.toml").exists(), (
    f"REPO_ROOT did not resolve to the repo root: {REPO_ROOT}"
)

README_PATH = REPO_ROOT / "README.md"
PIP_INSTALL_GUIDE_PATH = REPO_ROOT / "doc" / "PIP_INSTALL_GUIDE.md"


@pytest.mark.parametrize(
    "expected_substring",
    [
        "pow-rag-mcp",
        "uvx --from pow-rag-mcp",
        "uv tool install pow-rag-mcp",
        "pip install pow-rag-mcp",
        ">=3.11",
    ],
    ids=[
        "distribution_name",
        "uvx_command",
        "uv_tool_install_command",
        "pip_install_command",
        "requires_python_version",
    ],
)
def test_readme_contains_expected_substring(expected_substring):
    """README.md should contain each required string/command/version marker."""
    content = README_PATH.read_text(encoding="utf-8")
    assert expected_substring in content, (
        f"README.md missing {expected_substring!r}"
    )
    print(f"  PASS: README.md contains {expected_substring!r}")


def test_readme_consistency_with_pyproject_toml():
    """README.md requires-python string should match pyproject.toml."""
    readme_content = README_PATH.read_text(encoding="utf-8")
    pyproject_content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    # Extract requires-python from pyproject.toml
    pyproject_match = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject_content)
    assert pyproject_match, "pyproject.toml missing requires-python"
    expected_version = pyproject_match.group(1)

    assert expected_version in readme_content, (
        f"README.md missing requires-python value '{expected_version}' from pyproject.toml"
    )
    print(f"  PASS: README.md requires-python matches pyproject.toml ({expected_version})")


def test_pip_install_guide_has_public_pypi_heading():
    """PIP_INSTALL_GUIDE.md should have a distinct heading for the public-PyPI flow."""
    content = PIP_INSTALL_GUIDE_PATH.read_text(encoding="utf-8")

    # Check for the main heading for public PyPI install
    assert "## Public PyPI install" in content, (
        "PIP_INSTALL_GUIDE.md missing '## Public PyPI install' heading"
    )
    print("  PASS: PIP_INSTALL_GUIDE.md has 'Public PyPI install' heading")


def test_pip_install_guide_has_local_index_heading():
    """PIP_INSTALL_GUIDE.md should have a distinct heading for the local-index flow."""
    content = PIP_INSTALL_GUIDE_PATH.read_text(encoding="utf-8")

    # Check for the main heading for local index install
    assert "## Local index install" in content, (
        "PIP_INSTALL_GUIDE.md missing '## Local index install' heading"
    )
    print("  PASS: PIP_INSTALL_GUIDE.md has 'Local index install' heading")


def test_pip_install_guide_headings_are_distinct():
    """Public PyPI and local index headings should be separate sections."""
    content = PIP_INSTALL_GUIDE_PATH.read_text(encoding="utf-8")

    # Extract heading positions
    public_idx = content.find("## Public PyPI install")
    local_idx = content.find("## Local index install")

    assert public_idx != -1 and local_idx != -1, (
        "Missing one or both required headings"
    )
    assert abs(public_idx - local_idx) > 100, (
        "Public PyPI and Local index sections appear to be merged or too close together"
    )
    print("  PASS: Public PyPI and Local index headings are distinct sections")
