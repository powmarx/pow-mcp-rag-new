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


def test_default_config_path_uses_xdg_config_home():
    """XDG_CONFIG_HOME override should be respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmpdir}):
            cfg = paths.default_config_path()
    assert str(tmpdir) in str(cfg)
    assert "rag-mcp" in str(cfg)
    assert cfg.name == "config.yaml"
    print(f"  PASS: XDG_CONFIG_HOME override respected")


def test_default_data_path_uses_localappdata_on_windows():
    """On Windows, data should land in %LOCALAPPDATA%/rag-mcp/."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")
    data = paths.default_data_path()
    local = os.environ.get("LOCALAPPDATA", "")
    assert local in str(data)
    assert "rag-mcp" in str(data)
    print(f"  PASS: data path = {data}")


def test_default_data_path_uses_xdg_data_home():
    """XDG_DATA_HOME override should be respected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"XDG_DATA_HOME": tmpdir}):
            data = paths.default_data_path()
    assert str(tmpdir) in str(data)
    assert "rag-mcp" in str(data)
    print(f"  PASS: XDG_DATA_HOME override respected")


# =============================================================================
# 2. Env var overrides
# =============================================================================

def test_rag_config_path_env_overrides_default():
    """RAG_CONFIG_PATH env var should bypass XDG resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom = Path(tmpdir) / "custom_config.yaml"
        custom.write_text("projects: []\n", encoding="utf-8")
        with patch.dict(os.environ, {"RAG_CONFIG_PATH": str(custom)}):
            result = paths.resolve_config_path()
    assert result == custom
    print(f"  PASS: RAG_CONFIG_PATH override = {result}")


def test_rag_data_path_env_overrides_default():
    """RAG_DATA_PATH env var should bypass XDG resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"RAG_DATA_PATH": tmpdir}):
            result = paths.resolve_data_path()
    assert str(result) == tmpdir
    print(f"  PASS: RAG_DATA_PATH override = {result}")


def test_empty_rag_config_path_falls_back_to_xdg():
    """Empty RAG_CONFIG_PATH should fall back to XDG path."""
    env = {k: v for k, v in os.environ.items() if k not in ("RAG_CONFIG_PATH", "XDG_CONFIG_HOME")}
    with tempfile.TemporaryDirectory() as tmpdir:
        env["XDG_CONFIG_HOME"] = tmpdir
        # Pre-create the config so resolve doesn't seed
        cfg = Path(tmpdir) / "rag-mcp" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("projects: []\n", encoding="utf-8")
        env["RAG_CONFIG_PATH"] = ""
        with patch.dict(os.environ, env, clear=True):
            result = paths.resolve_config_path()
    assert "rag-mcp" in str(result)
    assert result.name == "config.yaml"
    print(f"  PASS: empty RAG_CONFIG_PATH falls back to XDG")


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
    """rag-mcp script should exist in the venv Scripts/bin directory."""
    scripts_dir = Path(sys.executable).parent
    candidates = [
        scripts_dir / "rag-mcp",
        scripts_dir / "rag-mcp.exe",
    ]
    found = any(p.exists() for p in candidates)
    assert found, f"rag-mcp script not found in {scripts_dir}"
    print(f"  PASS: CLI script found in {scripts_dir}")

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