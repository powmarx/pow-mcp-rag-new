"""
ChromaDB / indexing diagnostic tool for the rag-mcp-new-pip data volume.

Designed to run *inside* the rag-mcp-new-pip:latest container (it needs chromadb +
the baked config), against the same `rag-mcp-new-pip-data` volume the MCP server
uses. It never starts the MCP server itself, so it's safe to run concurrently
with a live Kiro session.

What it checks:
  1. SQLite integrity of chroma.sqlite3 (PRAGMA integrity_check).
  2. Collections registered in config.yaml vs collections that actually
     exist in ChromaDB (name drift / orphans on either side).
  3. For every collection: get_collection() + count() + query() executed in
     an ISOLATED subprocess with a timeout. This is deliberate — a corrupted
     HNSW vector index crashes the whole process with a native SIGSEGV
     (exit 139) the moment it's touched, so each collection must be probed
     in its own throwaway process or one bad collection kills the whole scan.
  4. Cross-check: rows in the `embeddings` sqlite table per segment vs the
     count() reported by the API (catches metadata/vector desync).
  5. Orphaned segment directories on disk (present in /app/data but not
     referenced by any row in the `segments` table) and missing header.bin
     files for registered segments.

Usage (from the host, Windows/PowerShell):

    docker run --rm -v rag-mcp-new-pip-data:/app/data -v "${PWD}/scripts/diagnose_chromadb.py:/tmp/diag.py:ro" ^
        rag-mcp-new-pip:latest python /tmp/diag.py

    # Restrict to one project:
    docker run --rm -v rag-mcp-new-pip-data:/app/data -v "${PWD}/scripts/diagnose_chromadb.py:/tmp/diag.py:ro" ^
        rag-mcp-new-pip:latest python /tmp/diag.py --project my-project

Or use the bundled scripts/diagnose-chromadb.ps1 wrapper which mounts the
volume for you.

Exit code is 0 if everything is healthy, 1 if any problem was found.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DATA_PATH = Path(os.environ.get("RAG_DATA_PATH", "/app/data"))
DB_PATH = DATA_PATH / "chroma.sqlite3"
CONFIG_PATH = Path(os.environ.get("RAG_CONFIG_PATH") or (DATA_PATH / "config.yaml"))
COLLECTION_TIMEOUT_S = 30

ISSUES: list[str] = []


def flag(msg: str) -> None:
    ISSUES.append(msg)
    print(f"  [ISSUE] {msg}")


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. SQLite integrity
# ---------------------------------------------------------------------------

def check_sqlite_integrity(conn: sqlite3.Connection) -> None:
    section("SQLite integrity (chroma.sqlite3)")
    if not DB_PATH.exists():
        flag(f"Database file not found at {DB_PATH}")
        return
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    print(f"  db size: {size_mb:.1f} MB")
    cur = conn.cursor()
    cur.execute("PRAGMA integrity_check")
    result = cur.fetchall()
    if result == [("ok",)]:
        ok("PRAGMA integrity_check passed")
    else:
        flag(f"PRAGMA integrity_check reported problems: {result[:10]}")


# ---------------------------------------------------------------------------
# 2. config.yaml projects vs actual collections
# ---------------------------------------------------------------------------

def load_config_projects() -> list[str]:
    if not CONFIG_PATH.exists():
        flag(f"config.yaml not found at {CONFIG_PATH}")
        return []
    try:
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        prefix = (cfg.get("storage") or {}).get("collection_prefix", "rag")
        names = []
        for p in cfg.get("projects", []):
            if p.get("removed"):
                continue
            names.append((p["name"], f"{prefix}_{p['name']}"))
        return names
    except Exception as e:
        flag(f"Failed to parse config.yaml: {e}")
        return []


def check_project_collection_drift(conn: sqlite3.Connection, project_filter: str | None) -> list[tuple[str, str]]:
    section("config.yaml projects vs ChromaDB collections")
    cur = conn.cursor()
    cur.execute("SELECT name FROM collections")
    actual = {row[0] for row in cur.fetchall()}

    configured = load_config_projects()
    if project_filter:
        configured = [(n, c) for (n, c) in configured if n == project_filter]

    configured_collection_names = {c for _, c in configured}

    for name, collection_name in configured:
        if collection_name in actual:
            ok(f"{name} -> {collection_name} (present)")
        else:
            flag(f"{name} configured but collection '{collection_name}' does NOT exist in ChromaDB "
                 f"(name drift or never indexed — see doc/TROUBLESHOOTING.md '--project silently does nothing')")

    orphans = actual - configured_collection_names
    for orphan in orphans:
        print(f"  [info] collection '{orphan}' exists in ChromaDB but has no matching entry in config.yaml "
              f"(may be fine if it belongs to a project not in this config, e.g. a different config version)")

    # Return the list of (project_name, collection_name) pairs actually present, to probe.
    to_probe = [(n, c) for (n, c) in configured if c in actual]
    if not to_probe and not project_filter:
        cur.execute("SELECT name FROM collections")
        to_probe = [(row[0], row[0]) for row in cur.fetchall()]
    return to_probe


# ---------------------------------------------------------------------------
# 3. Orphaned segment directories / missing header.bin
# ---------------------------------------------------------------------------

def check_segment_filesystem(conn: sqlite3.Connection) -> None:
    section("Segment directories on disk vs sqlite `segments` table")
    cur = conn.cursor()
    cur.execute("SELECT id, type, collection FROM segments")
    segments = cur.fetchall()
    known_ids = {row[0] for row in segments}

    for seg_id, seg_type, _collection in segments:
        seg_dir = DATA_PATH / seg_id
        if "hnsw" in seg_type:
            if not seg_dir.exists():
                flag(f"Vector segment dir missing on disk: {seg_id} ({seg_type})")
                continue
            header = seg_dir / "header.bin"
            data0 = seg_dir / "data_level0.bin"
            if not header.exists():
                flag(f"Missing header.bin for segment {seg_id}")
            if not data0.exists():
                flag(f"Missing data_level0.bin for segment {seg_id}")
            elif data0.stat().st_size == 0:
                flag(f"data_level0.bin is empty (0 bytes) for segment {seg_id}")

    on_disk_dirs = {
        p.name for p in DATA_PATH.iterdir()
        if p.is_dir() and p.name not in ("chroma", "pdf_cache")
    }
    orphan_dirs = on_disk_dirs - known_ids
    for d in orphan_dirs:
        print(f"  [info] directory '{d}' on disk has no matching row in `segments` table "
              f"(leftover from a deleted collection — safe to ignore, or clean up manually)")

    if not any("Missing" in i or "empty" in i for i in ISSUES):
        ok(f"{len(segments)} segment(s) checked, all files present")


# ---------------------------------------------------------------------------
# 4 & 5. Per-collection isolated probe (count / query) + row cross-check
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = r"""
import sys, json
import chromadb

collection_name = sys.argv[1]
data_path = sys.argv[2]

result = {"stage": "connect"}
try:
    client = chromadb.PersistentClient(path=data_path)
    result["stage"] = "get_collection"
    col = client.get_collection(name=collection_name)
    result["stage"] = "count"
    n = col.count()
    result["count"] = n
    result["stage"] = "peek"
    col.peek(limit=1)
    result["stage"] = "query"
    dim = col._model.dimension if hasattr(col, "_model") else None
    # Fall back: infer dimension from a peek result if available.
    peeked = col.peek(limit=1)
    embeddings = peeked.get("embeddings")
    if embeddings is not None and len(embeddings) > 0 and embeddings[0] is not None:
        dim = len(embeddings[0])
    if dim:
        col.query(query_embeddings=[[0.0] * dim], n_results=1)
    result["stage"] = "done"
    result["ok"] = True
except Exception as e:
    result["ok"] = False
    result["error"] = f"{type(e).__name__}: {e}"

print(json.dumps(result))
"""


def probe_collection(collection_name: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT, collection_name, str(DATA_PATH)],
        capture_output=True,
        text=True,
        timeout=COLLECTION_TIMEOUT_S,
    )
    if proc.returncode != 0:
        # Negative on POSIX means killed by signal (e.g. -11 = SIGSEGV).
        sig = -proc.returncode if proc.returncode < 0 else proc.returncode
        return {
            "ok": False,
            "crashed": True,
            "returncode": proc.returncode,
            "signal_hint": sig,
            "stderr": proc.stderr[-2000:],
        }
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "crashed": False, "error": "no JSON output", "stdout": proc.stdout, "stderr": proc.stderr}


def check_collections(to_probe: list[tuple[str, str]]) -> None:
    section("Per-collection isolated probe (count / peek / query)")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    for project_name, collection_name in to_probe:
        cur.execute("SELECT id FROM collections WHERE name = ?", (collection_name,))
        row = cur.fetchone()
        sqlite_embedding_rows = None
        if row:
            collection_id = row[0]
            cur.execute(
                "SELECT id FROM segments WHERE collection = ? AND type LIKE '%metadata%'",
                (collection_id,),
            )
            seg_row = cur.fetchone()
            if seg_row:
                cur.execute("SELECT COUNT(*) FROM embeddings WHERE segment_id = ?", (seg_row[0],))
                sqlite_embedding_rows = cur.fetchone()[0]

        print(f"\n  probing '{collection_name}' (project: {project_name})...")
        start = time.time()
        try:
            result = probe_collection(collection_name)
        except subprocess.TimeoutExpired:
            flag(f"{collection_name}: TIMEOUT after {COLLECTION_TIMEOUT_S}s (hung — possibly deadlocked or extremely large index)")
            continue
        elapsed = time.time() - start

        if result.get("crashed"):
            flag(
                f"{collection_name}: CRASHED (exit code {result['returncode']}, likely SIGSEGV) "
                f"at stage='{result.get('stage', '?')}' — corrupted HNSW vector index. "
                f"Fix: docker run --rm -v \"$SRC:/projects:ro\" -v rag-mcp-new-pip-data:/app/data "
                f"rag-mcp-new-pip:latest python indexer.py --reset --project {project_name}"
            )
            if result.get("stderr"):
                print(f"    stderr tail: {result['stderr'][-500:]}")
            continue

        if not result.get("ok"):
            flag(f"{collection_name}: ERROR at stage='{result.get('stage', '?')}': {result.get('error')}")
            continue

        count = result.get("count")
        ok(f"{collection_name}: healthy ({count} chunks, checked in {elapsed:.1f}s)")

        if sqlite_embedding_rows is not None and count is not None and sqlite_embedding_rows != count:
            flag(
                f"{collection_name}: count() returned {count} but metadata segment sqlite table has "
                f"{sqlite_embedding_rows} rows — possible desync between metadata and vector segments"
            )

    conn.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose ChromaDB / indexing issues in rag-mcp-new-pip-data")
    parser.add_argument("--project", type=str, default=None, help="Restrict checks to a single project name")
    parser.add_argument("--skip-probe", action="store_true", help="Skip the slow per-collection count/query probe")
    args = parser.parse_args()

    print(f"Data path:   {DATA_PATH}")
    print(f"Config path: {CONFIG_PATH}")

    if not DATA_PATH.exists():
        print(f"[fatal] Data path {DATA_PATH} does not exist. Is the volume mounted?")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        check_sqlite_integrity(conn)
        to_probe = check_project_collection_drift(conn, args.project)
        check_segment_filesystem(conn)
    finally:
        conn.close()

    if not args.skip_probe:
        check_collections(to_probe)
    else:
        print("\n(skipped per-collection probe: --skip-probe)")

    section("Summary")
    if ISSUES:
        print(f"{len(ISSUES)} issue(s) found:")
        for i in ISSUES:
            print(f"  - {i}")
        return 1
    else:
        print("No issues found. ChromaDB and indexing state look healthy.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
