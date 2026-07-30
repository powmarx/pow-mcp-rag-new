"""Tests for the ConfigLoader module."""

import sys
import tempfile
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
assert (_SRC_DIR.parent / "pyproject.toml").exists(), (
    f"_SRC_DIR's parent did not resolve to the repo root: {_SRC_DIR.parent}"
)
sys.path.insert(0, str(_SRC_DIR))

from rag_mcp.config_loader import ConfigLoader, AppConfig


SAMPLE_CONFIG = """
embedding:
  model: "sentence-transformers/all-MiniLM-L6-v2"

storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
  url: ""

chunking:
  chunk_size: 500
  chunk_overlap: 100
  separators:
    - "\\n## "
    - "\\n\\n"

discovery_ignore:
  - "tools-"

projects:
  - name: "test-project"
    description: "A test project"
    base_path: "."
    sources:
      - pattern: "*.py"
        type: "source"
        description: "Python files"
"""


def test_load_valid_config():
    """Should load a valid config file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG)
        f.flush()
        path = Path(f.name)

    loader = ConfigLoader(path)
    config = loader.load()

    assert config.embedding.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert config.storage.collection_prefix == "test_rag"
    assert config.chunking.chunk_size == 500
    assert config.chunking.chunk_overlap == 100
    assert len(config.projects) == 1
    assert config.projects[0].name == "test-project"
    assert len(config.projects[0].sources) == 1
    assert config.projects[0].sources[0].pattern == "*.py"
    assert config.discovery_ignore == ["tools-"]
    path.unlink()


def test_load_missing_file_raises():
    """Should raise FileNotFoundError for missing config."""
    loader = ConfigLoader(Path("/nonexistent/config.yaml"))
    try:
        loader.load()
        assert False, "Should have raised"
    except FileNotFoundError:
        pass


def test_save_and_reload():
    """Should save config and reload it identically."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG)
        f.flush()
        path = Path(f.name)

    loader = ConfigLoader(path)
    config = loader.load()

    # Modify and save
    config.chunking.chunk_size = 2000
    config.discovery_ignore.append("test-")
    loader.save(config)

    # Reload
    config2 = loader.load()
    assert config2.chunking.chunk_size == 2000
    assert "test-" in config2.discovery_ignore
    assert config2.projects[0].name == "test-project"
    path.unlink()


def test_expand_path_tilde():
    """Should expand ~ to home directory."""
    loader = ConfigLoader(Path("dummy.yaml"))
    expanded = loader.expand_path("~/projects/test")
    assert "~" not in expanded
    assert Path(expanded).is_absolute()


def test_expand_path_env_var(monkeypatch=None):
    """Should expand ${VAR} environment variables."""
    import os
    os.environ["TEST_RAG_ROOT"] = "/tmp/test_root"

    loader = ConfigLoader(Path("dummy.yaml"))
    expanded = loader.expand_path("${TEST_RAG_ROOT}/my-project")
    assert "TEST_RAG_ROOT" not in expanded
    assert "test_root" in expanded

    del os.environ["TEST_RAG_ROOT"]


if __name__ == "__main__":
    test_load_valid_config()
    test_load_missing_file_raises()
    test_save_and_reload()
    test_expand_path_tilde()
    test_expand_path_env_var()
    print("All config_loader tests passed!")


# --- Log config validation tests (Task 1.3) ---

LOG_CONFIG_TEMPLATE = """
embedding:
  model: "sentence-transformers/all-MiniLM-L6-v2"
storage:
  path: "./data"
  collection_prefix: "test_rag"
  mode: "local"
chunking:
  chunk_size: 500
  chunk_overlap: 100
projects:
  - name: "log_project"
    description: "Test log project"
    base_path: "."
    sources:
      - pattern: "*.log"
        type: "log"
        description: "Log files"
        log_patterns:
{log_patterns}
    log_settings:
{log_settings}
"""


def _make_config(log_patterns: str = "", log_settings: str = "") -> Path:
    """Write a config YAML and return the path."""
    content = LOG_CONFIG_TEMPLATE.format(
        log_patterns=log_patterns or "          []",
        log_settings=log_settings or "      group_time_window_ms: 500\n      max_continuation_lines: 500\n      max_group_lines: 500\n      dedup_threshold: 3",
    )
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(content)
    f.flush()
    f.close()
    return Path(f.name)


def test_valid_log_patterns_regex():
    """Valid regex in log_patterns should load without error."""
    patterns = """
          - name: "test_pattern"
            regex: '(?P<timestamp>\\d{2}:\\d{2}:\\d{2})\\s+(?P<message>.*)'
            event_type: "info"
            priority: 100
"""
    path = _make_config(log_patterns=patterns)
    try:
        loader = ConfigLoader(path)
        config = loader.load()
        assert len(config.projects[0].sources[0].log_patterns) == 1
        assert config.projects[0].sources[0].log_patterns[0].name == "test_pattern"
    finally:
        path.unlink()


@pytest.mark.parametrize(
    "config_snippet, expected_match_regex",
    [
        (
            {
                "log_patterns": """
          - name: "bad_pattern"
            regex: '(?P<timestamp>[unclosed'
            event_type: "info"
            priority: 100
"""
            },
            "Invalid regex",
        ),
        (
            {
                "log_settings": """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
      line_filters:
        - name: "bad_filter"
          action: "exclude"
          match: '(unclosed[bracket'
          priority: 100
"""
            },
            "Invalid regex.*line_filters",
        ),
        (
            {
                "log_settings": """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
      content_transforms:
        - name: "bad_transform"
          match: '(?P<broken'
          action: "strip"
          priority: 100
"""
            },
            "Invalid regex.*content_transforms",
        ),
        (
            {
                "log_settings": """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
      grouping_rules:
        - name: "bad_group"
          start_pattern: '[invalid regex('
          continuation_patterns: []
"""
            },
            "Invalid regex.*grouping_rules.*start_pattern",
        ),
        (
            {
                "log_settings": """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
      grouping_rules:
        - name: "test_group"
          start_pattern: '.*BEGIN.*'
          continuation_patterns:
            - '.*valid.*'
            - '(broken[regex'
"""
            },
            "Invalid regex.*grouping_rules.*continuation_patterns",
        ),
    ],
    ids=[
        "invalid_log_pattern_regex",
        "invalid_line_filter_regex",
        "invalid_content_transform_regex",
        "invalid_grouping_rule_start_pattern",
        "invalid_grouping_rule_continuation_pattern",
    ],
)
def test_invalid_regex_raises(config_snippet, expected_match_regex):
    """Invalid regex anywhere in log_patterns/log_settings should raise ValueError at load time."""
    path = _make_config(**config_snippet)
    try:
        loader = ConfigLoader(path)
        with pytest.raises(ValueError, match=expected_match_regex):
            loader.load()
    finally:
        path.unlink()


def test_duplicate_log_pattern_name_raises():
    """Duplicate name within same source's log_patterns should raise ValueError."""
    patterns = """
          - name: "dup_name"
            regex: '(?P<timestamp>\\d{2}:\\d{2}:\\d{2})\\s+(?P<message>.*)'
            event_type: "info"
            priority: 100
          - name: "dup_name"
            regex: '(?P<timestamp>\\d{4}-\\d{2}-\\d{2})\\s+(?P<message>.*)'
            event_type: "warning"
            priority: 200
"""
    path = _make_config(log_patterns=patterns)
    try:
        loader = ConfigLoader(path)
        with pytest.raises(ValueError, match="duplicate log_patterns name"):
            loader.load()
    finally:
        path.unlink()


def test_log_patterns_max_50_raises():
    """More than 50 log_patterns entries should raise ValueError."""
    # Generate 51 unique patterns
    lines = []
    for i in range(51):
        lines.append(
            f"          - name: \"pattern_{i}\"\n"
            f"            regex: '(?P<timestamp>\\d{{2}}:\\d{{2}}:\\d{{2}})\\s+(?P<message>.*)'\n"
            f"            event_type: \"info\"\n"
            f"            priority: {i + 1}"
        )
    patterns = "\n".join(lines)
    path = _make_config(log_patterns=patterns)
    try:
        loader = ConfigLoader(path)
        with pytest.raises(ValueError, match="exceeds maximum of 50"):
            loader.load()
    finally:
        path.unlink()


def test_invalid_event_type_format_raises():
    """event_type with invalid characters should raise ValueError."""
    patterns = """
          - name: "test_pattern"
            regex: '(?P<timestamp>\\d{2}:\\d{2}:\\d{2})\\s+(?P<message>.*)'
            event_type: "invalid-type!"
            priority: 100
"""
    path = _make_config(log_patterns=patterns)
    try:
        loader = ConfigLoader(path)
        with pytest.raises(ValueError, match="event_type"):
            loader.load()
    finally:
        path.unlink()


@pytest.mark.parametrize(
    "log_settings_snippet, expected_match_regex",
    [
        (
            """
      group_time_window_ms: 5
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
""",
            "group_time_window_ms",
        ),
        (
            """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 1
""",
            "dedup_threshold",
        ),
    ],
    ids=[
        "numeric_range_validation",
        "dedup_threshold_too_low",
    ],
)
def test_log_settings_validation_raises(log_settings_snippet, expected_match_regex):
    """Numeric fields outside valid ranges should raise ValueError."""
    path = _make_config(log_settings=log_settings_snippet)
    try:
        loader = ConfigLoader(path)
        with pytest.raises(ValueError, match=expected_match_regex):
            loader.load()
    finally:
        path.unlink()


def test_valid_line_filters_and_transforms_load():
    """Valid line_filters and content_transforms with valid regex should load correctly."""
    settings = """
      group_time_window_ms: 500
      max_continuation_lines: 500
      max_group_lines: 500
      dedup_threshold: 3
      line_filters:
        - name: "exclude_hex"
          action: "exclude"
          match: '^\\s*[0-9A-Fa-f]{4}:'
          priority: 100
      content_transforms:
        - name: "strip_pointers"
          match: '0x[0-9A-Fa-f]{8}'
          action: "strip"
          priority: 200
      grouping_rules:
        - name: "cmd_lifecycle"
          start_pattern: '.*executeCMD Begin.*'
          continuation_patterns:
            - '.*executeCMD End.*'
"""
    path = _make_config(log_settings=settings)
    try:
        loader = ConfigLoader(path)
        config = loader.load()
        log_settings = config.projects[0].log_settings
        assert log_settings is not None
        assert len(log_settings.line_filters) == 1
        assert log_settings.line_filters[0].name == "exclude_hex"
        assert len(log_settings.content_transforms) == 1
        assert log_settings.content_transforms[0].name == "strip_pointers"
        assert len(log_settings.grouping_rules) == 1
        assert log_settings.grouping_rules[0].name == "cmd_lifecycle"
    finally:
        path.unlink()
