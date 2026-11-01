"""Tests that the observability layer is documented and the doc is bundled.

  * doc/OBSERVABILITY.md exists and covers the log file, the format, the
    counters, the MCP tool and the CLI
  * it is bundled: listed in scripts/sync_package_data.py, in cli._BUNDLED_DOCS,
    copied into src/rag_mcp/data/docs/ and in sync with doc/
  * `rag-mcp docs observability` prints it
  * CLI_REFERENCE.md / TOOLS_GUIDE.md / README.md reference the new command/tool
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from rag_mcp import cli, observability

DOC = _REPO_ROOT / "doc" / "OBSERVABILITY.md"
BUNDLED = _REPO_ROOT / "src" / "rag_mcp" / "data" / "docs" / "OBSERVABILITY.md"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        observability.ENV_LOG_PATH,                       # env override documented
        str(observability.DEFAULT_LOG_RELATIVE_PATH),     # observability/events.jsonl
        "## Log format",
        '"event":"tool_call"', '"event":"embed_failure"', '"event":"log_epoch_backfill"',
        "## Counters",
        "zero_result_calls", "records_updated_total", "by_error_type",
        "## `get_metrics` MCP tool",
        '"source": "live"', '"source": "jsonl_replay"',
        "## `rag-mcp get_metrics` CLI",
        "--json", "--file PATH",
        "No rotation",
    ],
)
def test_observability_doc_covers(expected):
    assert expected in _doc(), f"OBSERVABILITY.md missing {expected!r}"


def test_documented_field_lists_match_the_emitted_records(capsys, tmp_path):
    """Every field each event type actually emits is mentioned in the doc."""
    import time
    from unittest.mock import MagicMock
    from rag_mcp.embedding_generator import EmbeddingGenerator

    observability.configure(None)
    tool = observability.log_tool_call_event("search_docs", {"query": "q"}, time.time(), 1)
    gen = EmbeddingGenerator("m")
    gen.model = MagicMock()
    gen.model.encode.side_effect = RuntimeError("x")
    with pytest.raises(RuntimeError):
        gen.encode(["a"])
    backfill = observability.log_log_epoch_backfill_event("rag_logs", 1, 1, 1, time.time())
    capsys.readouterr()

    doc = _doc()
    embed_fields = {"kind", "batch_size", "model", "error_type", "error"}
    for field in set(tool) | embed_fields | set(backfill):
        assert f"`{field}`" in doc, f"field {field!r} emitted but not documented"


def test_documented_cli_flags_exist():
    """Flags in the doc are the flags the CLI parses (and vice versa)."""
    src = (_REPO_ROOT / "src" / "rag_mcp" / "cli.py").read_text(encoding="utf-8")
    for flag in ("--json", "--file"):
        assert flag in _doc() and f'"{flag}"' in src


# ---------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------


def test_doc_is_listed_for_bundling():
    sync_src = (_REPO_ROOT / "scripts" / "sync_package_data.py").read_text(encoding="utf-8")
    assert '"OBSERVABILITY.md"' in sync_src
    assert cli._BUNDLED_DOCS.get("observability") == "OBSERVABILITY.md"


def test_bundled_copy_is_in_sync_with_doc():
    assert BUNDLED.exists(), "run: python scripts/sync_package_data.py"
    assert BUNDLED.read_text(encoding="utf-8") == _doc()


def test_rag_mcp_docs_observability_prints_the_guide(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag-mcp", "docs", "observability"])
    cli.main()
    out = capsys.readouterr().out
    assert out.startswith("# Observability Guide")
    assert "rag-mcp get_metrics" in out


def test_rag_mcp_docs_lists_observability(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rag-mcp", "docs"])
    cli.main()
    out = capsys.readouterr().out
    assert "observability" in out and "OBSERVABILITY.md" in out
    assert "OBSERVABILITY.md  [missing]" not in out


# ---------------------------------------------------------------------------
# Cross-references in the existing docs
# ---------------------------------------------------------------------------


def test_cli_reference_documents_get_metrics_and_env_var():
    text = (_REPO_ROOT / "doc" / "CLI_REFERENCE.md").read_text(encoding="utf-8")
    assert "## `get_metrics`" in text
    assert "rag-mcp get_metrics --json" in text
    assert observability.ENV_LOG_PATH in text
    assert "rag-mcp docs observability" in text


def test_tools_guide_documents_get_metrics_tool():
    text = (_REPO_ROOT / "doc" / "TOOLS_GUIDE.md").read_text(encoding="utf-8")
    assert "| 20 | [`get_metrics`]" in text
    assert "### 20. get_metrics" in text
    assert "OBSERVABILITY.md" in text


def test_readme_links_the_observability_guide():
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "[doc/OBSERVABILITY.md](doc/OBSERVABILITY.md)" in text
    assert "`rag-mcp docs observability`" in text
