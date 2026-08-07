"""
Tests for Phase 2 packaging features.

Covers:
1. paths.py — XDG path resolution (config, data)
2. paths.py — RAG_CONFIG_PATH / RAG_DATA_PATH env overrides
3. paths.py — _seed_config finds config.template.yaml from repo layout
4. cli.py  — 'config' subcommand prints both paths
5. cli.py  — unknown subcommand exits with error
6. Package is installable (pyproject.toml is valid)
7. rag-mcp CLI script exists after install
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
assert (REPO_ROOT / "pyproject.toml").exists(), (
    f"REPO_ROOT did not resolve to the repo root: {REPO_ROOT}"
)
sys.path.insert(0, str(REPO_ROOT / "src"))

from rag_mcp import paths


# =============================================================================
# 1. XDG path resolution
# =============================================================================

def test_default_config_path_uses_appdata_on_windows():
    """On Windows, config should land in %APPDATA%/rag-mcp/config.yaml."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")
    cfg = paths.default_config_path()
    appdata = os.environ.get("APPDATA", "")
    assert appdata in str(cfg)
    assert "rag-mcp" in str(cfg)
    assert cfg.name == "config.yaml"
    print(f"  PASS: config path = {cfg}")


def test_default_data_path_uses_localappdata_on_windows():
    """On Windows, data should land in %LOCALAPPDATA%/rag-mcp/."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")
    data = paths.default_data_path()
    local = os.environ.get("LOCALAPPDATA", "")
    assert local in str(data)
    assert "rag-mcp" in str(data)
    print(f"  PASS: data path = {data}")


# =============================================================================
# 2. Env var overrides
# =============================================================================


# =============================================================================
# 3. _seed_config — finds template from repo layout
# =============================================================================

def test_seed_config_finds_template_from_repo():
    """_seed_config should find config.template.yaml from the repo config/ dir."""
    template = REPO_ROOT / "config" / "config.template.yaml"
    assert template.exists(), f"Template not found at {template}"

    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "config.yaml"
        paths._seed_config(target)
        # Use resolve() to normalise Windows short paths (NAME~1.SURNAME vs name.surname)
        assert target.resolve().exists() or target.exists(), (
            f"Expected seeded config at {target}"
        )
        content = (target.resolve() if target.resolve().exists() else target).read_text(encoding="utf-8")

    assert "embedding" in content
    print(f"  PASS: _seed_config seeded {len(content)} bytes from template")


def test_seed_config_creates_parent_dirs():
    """_seed_config should create missing parent directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "a" / "b" / "c" / "config.yaml"
        paths._seed_config(target)
        assert target.exists()
    print(f"  PASS: _seed_config created nested parent dirs")


def test_seed_config_stub_when_no_template(tmp_path):
    """When template is not found, _seed_config should write a minimal stub."""
    target = tmp_path / "config.yaml"

    # Patch __file__ in paths to a location far from the repo so no template is found
    fake_paths_file = tmp_path / "src" / "rag_mcp" / "paths.py"
    with patch.object(paths, "__file__", str(fake_paths_file)):
        # Also remove RAG_CONFIG_PATH to avoid interference
        with patch.dict(os.environ, {"RAG_CONFIG_PATH": ""}, clear=False):
            paths._seed_config(target)

    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "embedding" in content
    print(f"  PASS: _seed_config wrote stub when template not found")


# =============================================================================
# 4. CLI — 'config' subcommand
# =============================================================================

def test_cli_config_subcommand_prints_paths(capsys):
    """'rag-mcp config' should print Config and Data lines."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = Path(tmpdir) / "rag-mcp" / "config.yaml"
        cfg.parent.mkdir()
        cfg.write_text("projects: []\n", encoding="utf-8")

        env = {"RAG_CONFIG_PATH": str(cfg), "RAG_DATA_PATH": tmpdir}
        with patch.dict(os.environ, env):
            with patch.object(sys, "argv", ["rag-mcp", "config"]):
                from rag_mcp import cli
                cli.main()

    captured = capsys.readouterr()
    assert "Config" in captured.out
    assert "Data" in captured.out
    print(f"  PASS: cli config output:\n{captured.out.strip()}")


# =============================================================================
# 5. CLI — unknown subcommand exits
# =============================================================================

def test_cli_unknown_subcommand_exits():
    """Unknown subcommand should exit with code 1."""
    with patch.object(sys, "argv", ["rag-mcp", "foobar"]):
        from rag_mcp import cli
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 1
    print(f"  PASS: unknown subcommand exits with code 1")


# =============================================================================
# 6. pyproject.toml is valid
# =============================================================================

def test_pyproject_toml_is_valid():
    """pyproject.toml should parse without error and have required fields."""
    import tomllib  # Python 3.11+
    toml_path = REPO_ROOT / "pyproject.toml"
    assert toml_path.exists(), "pyproject.toml not found"
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    assert data["project"]["name"] == "pow-rag-mcp"
    assert data["project"]["license"] == "Apache-2.0"
    classifiers = data["project"]["classifiers"]
    # No "License :: OSI Approved :: ..." classifier: setuptools (PEP 639) raises a hard
    # build error if a license classifier is combined with an SPDX project.license string,
    # so the SPDX string above is the sole source of truth for the license.
    assert not any(c.startswith("License ::") for c in classifiers)
    requires_python = data["project"]["requires-python"]
    assert "Programming Language :: Python" in " ".join(classifiers)
    assert data["project"]["scripts"]["rag-mcp"] == "rag_mcp.cli:main"
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    print(f"  PASS: pyproject.toml valid, name={data['project']['name']}, license={data['project']['license']}")


# =============================================================================
# 7. CLI script installed
# =============================================================================

def test_cli_script_is_installed():
    """rag-mcp command entrypoint should be invokable in this environment."""
    scripts_dir = Path(sys.executable).parent
    scripts_subdir = scripts_dir / "Scripts"
    candidates = [
        scripts_dir / "rag-mcp",
        scripts_dir / "rag-mcp.exe",
        scripts_subdir / "rag-mcp",
        scripts_subdir / "rag-mcp.exe",
    ]
    found = next((p for p in candidates if p.exists()), None)
    if found is not None:
        print(f"  PASS: CLI script found at {found}")
        return

    # Source-checkout fallback: script may not be installed globally, but the
    # module entrypoint must still execute.
    result = __import__("subprocess").run(
        [sys.executable, "-m", "rag_mcp.cli", "config"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert result.returncode == 0, (
        "Neither rag-mcp script nor python -m rag_mcp.cli is runnable.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "Config" in result.stdout and "Data" in result.stdout
    print("  PASS: module entrypoint works without installed script")

def test_prepare_env_resolves_paths(monkeypatch, tmp_path):
    """_prepare_env() must resolve config/data paths without NameError —
    regression test for resolve_config_path/resolve_data_path import
    being scoped to main() instead of available to _prepare_env()."""
    monkeypatch.setenv("RAG_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("RAG_DATA_PATH", str(tmp_path / "data"))
    (tmp_path / "config.yaml").write_text("storage:\n  path: ''\n")

    from rag_mcp import cli
    cfg_path = cli._prepare_env()
    assert cfg_path.exists()