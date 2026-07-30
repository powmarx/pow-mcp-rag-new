"""Tests for the FileReader module."""

import sys
import tempfile
from pathlib import Path

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.file_reader import FileReader


def test_read_utf8_file():
    """Should read UTF-8 files correctly."""
    reader = FileReader()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("Hello UTF-8 world!")
        f.flush()
        path = Path(f.name)

    result = reader.read(path, path.parent)
    assert result is not None
    assert result.content == "Hello UTF-8 world!"
    assert result.is_pdf is False
    assert result.file_hash != ""
    path.unlink()


def test_read_latin1_file():
    """Should fall back to Latin-1 for non-UTF-8 files."""
    reader = FileReader()
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write("Olá mundo com acentuação".encode("latin-1"))
        f.flush()
        path = Path(f.name)

    result = reader.read(path, path.parent)
    assert result is not None
    assert "mundo" in result.content
    path.unlink()


def test_skip_binary_file():
    """Should return None for binary files."""
    reader = FileReader()
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
        f.write(b"\x00\x01\x02\x03\x04\x05binary content")
        f.flush()
        path = Path(f.name)

    result = reader.read(path, path.parent)
    assert result is None
    path.unlink()


def test_hash_deterministic():
    """Same file content should produce same hash."""
    reader = FileReader()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("deterministic content")
        f.flush()
        path = Path(f.name)

    hash1 = reader.compute_hash(path)
    hash2 = reader.compute_hash(path)
    assert hash1 == hash2
    assert len(hash1) == 32  # MD5 hex length
    path.unlink()


def test_hash_changes_with_content():
    """Different content should produce different hashes."""
    reader = FileReader()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("content A")
        f.flush()
        path = Path(f.name)

    hash1 = reader.compute_hash(path)

    with open(path, "w", encoding="utf-8") as f:
        f.write("content B")

    hash2 = reader.compute_hash(path)
    assert hash1 != hash2
    path.unlink()


def test_relative_path_uses_forward_slashes():
    """Relative path should use forward slashes regardless of OS."""
    reader = FileReader()
    with tempfile.TemporaryDirectory() as tmpdir:
        subdir = Path(tmpdir) / "sub" / "folder"
        subdir.mkdir(parents=True)
        filepath = subdir / "test.txt"
        filepath.write_text("test", encoding="utf-8")

        result = reader.read(filepath, Path(tmpdir))
        assert result is not None
        assert "\\" not in result.relative_path
        assert result.relative_path == "sub/folder/test.txt"


if __name__ == "__main__":
    test_read_utf8_file()
    test_read_latin1_file()
    test_skip_binary_file()
    test_hash_deterministic()
    test_hash_changes_with_content()
    test_relative_path_uses_forward_slashes()
    print("All file_reader tests passed!")
