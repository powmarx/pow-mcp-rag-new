"""Tests for LICENSE file presence and content, and README.md linking (Requirements 2.1, 2.4)."""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
assert (REPO_ROOT / "pyproject.toml").exists(), (
    f"REPO_ROOT did not resolve to the repo root: {REPO_ROOT}"
)


def test_license_file_exists():
    """LICENSE file should exist at repository root."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.exists(), "LICENSE file not found at repository root"


def test_license_contains_apache_license_text():
    """LICENSE file should contain 'Apache License' and 'Version 2.0'."""
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text, "LICENSE missing 'Apache License' text"
    assert "Version 2.0" in license_text, "LICENSE missing 'Version 2.0' text"


def test_license_no_placeholder_text():
    """LICENSE file should not contain [yyyy] or [name of copyright owner] placeholders."""
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "[yyyy]" not in license_text, "LICENSE contains [yyyy] placeholder"
    assert "[name of copyright owner]" not in license_text, "LICENSE contains [name of copyright owner] placeholder"


def test_license_file_is_complete():
    """LICENSE file should contain all required sections of Apache License 2.0."""
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    
    # Check for key sections that must be present
    required_sections = [
        "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
        "1. Definitions",
        "2. Grant of Copyright License",
        "3. Grant of Patent License",
        "4. Redistribution",
        "5. Submission of Contributions",
        "6. Trademarks",
        "7. Disclaimer of Warranty",
        "8. Limitation of Liability",
        "9. Accepting Warranty or Additional Liability",
        "END OF TERMS AND CONDITIONS",
        "Licensed under the Apache License, Version 2.0",
    ]
    
    for section in required_sections:
        assert section in license_text, f"LICENSE missing required section: {section}"


def test_readme_links_to_license():
    """README.md should contain a link to the LICENSE file."""
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "LICENSE" in readme_text, "README.md does not reference LICENSE file"
