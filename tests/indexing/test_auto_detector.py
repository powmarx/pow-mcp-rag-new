"""Tests for the ProjectAutoDetector module."""

import sys
import tempfile
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.auto_detector import ProjectAutoDetector


def _create_files(base_dir: Path, files_to_create: dict[str, str]) -> None:
    """Create files (and their parent dirs) under `base_dir` from a relative-path -> content map."""
    for relative_path, content in files_to_create.items():
        full_path = base_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)


@pytest.mark.parametrize(
    "files_to_create, expected_patterns",
    [
        (
            {
                "pyproject.toml": "[project]\nname='test'",
                "main.py": "print('hello')",
                "requirements.txt": "requests",
            },
            ["**/*.py", "pyproject.toml", "requirements.txt"],
        ),
        (
            {
                "go.mod": "module example.com/test",
                "main.go": "package main",
            },
            ["**/*.go", "go.mod"],
        ),
        (
            {
                "package.json": '{"name": "test"}',
                "tsconfig.json": "{}",
                "src/index.ts": "export default {}",
            },
            ["**/*.ts", "package.json", "tsconfig.json"],
        ),
        (
            {
                "doc/guide.md": "# Guide",
                "README.md": "# Project",
            },
            ["doc/**/*.md", "README.md"],
        ),
    ],
    ids=[
        "python_project",
        "go_project",
        "node_project",
        "common_docs",
    ],
)
def test_detect_stack(files_to_create: dict[str, str], expected_patterns: list[str]):
    """Should detect the expected patterns for each per-stack file layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        _create_files(path, files_to_create)

        detector = ProjectAutoDetector()
        sources = detector.detect(path)

        patterns = [s.pattern for s in sources]
        for expected_pattern in expected_patterns:
            assert expected_pattern in patterns


def test_detect_empty_dir_returns_empty():
    """Empty directory should return no patterns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        detector = ProjectAutoDetector()
        sources = detector.detect(Path(tmpdir))
        assert sources == []


def test_detect_gitmodules():
    """Should detect submodules from .gitmodules when no component/ dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        # Create .gitmodules
        (path / ".gitmodules").write_text(
            '[submodule "libs/mylib"]\n\tpath = libs/mylib\n\turl = git@example.com:mylib.git\n'
        )
        # Create the submodule directory with source files
        lib_dir = path / "libs" / "mylib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "mylib.h").write_text("#pragma once")
        (lib_dir / "mylib.cpp").write_text("void foo() {}")

        detector = ProjectAutoDetector()
        sources = detector.detect(path)

        patterns = [s.pattern for s in sources]
        assert "libs/mylib/**/*.h" in patterns
        assert "libs/mylib/**/*.cpp" in patterns


if __name__ == "__main__":
    for _files_to_create, _expected_patterns in test_detect_stack.pytestmark[0].args[1]:
        test_detect_stack(_files_to_create, _expected_patterns)
    test_detect_empty_dir_returns_empty()
    test_detect_gitmodules()
    print("All auto_detector tests passed!")
