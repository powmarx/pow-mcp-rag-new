"""
Shared fixtures for the mcp_tools test package.

The ``isolated_server_context`` fixture isolates each test from shared mutable
server state by snapshotting ``server.config.projects`` at the start of the test
and restoring it unconditionally on teardown — whether the test passed, failed,
or raised an unexpected exception.

Design notes
------------
* ``monkeypatch.setattr(server.config, "projects", list(server.config.projects))``
  replaces the projects list with a *copy* for the duration of the test.
  ``monkeypatch`` restores the original list object automatically at teardown,
  so any mutation (append, remove, attribute write) done during the test is
  completely invisible to subsequent tests.

  **Why monkeypatch and not a manual try/finally?**
  ``monkeypatch`` undoes the setattr even if the fixture itself raises, and it
  is called by pytest after all ``yield``-fixture teardowns complete — making it
  impossible to accidentally forget the restore.  The manual
  ``original = …; try: …; finally: server.x = original`` pattern in the existing
  ``TestRemoveProjectExecution`` / ``TestClearProjectIndexExecution`` tests is
  replaced by ``monkeypatch.setattr`` calls in tasks 8.2 / 8.3.

* Store / loader method replacements (``server.store.get_collection``,
  ``server.store.delete_collection``, ``server.loader.save``, …) should also go
  through the same ``monkeypatch`` fixture rather than hand-saved
  ``original = …`` variables, for the same reason.

* The fixture is *function-scoped* (default) so every test function gets its own
  independent copy of the projects list.

Usage
-----
Option 1 — per-test argument::

    def test_something(isolated_server_context):
        ...

Option 2 — whole-class decoration::

    @pytest.mark.usefixtures("isolated_server_context")
    class TestSomething:
        ...

Option 3 — module-level marker::

    pytestmark = pytest.mark.usefixtures("isolated_server_context")
"""

import pytest
import server
import rag_mcp.tools.management as _management


@pytest.fixture(autouse=True)
def _restore_management_ctx():
    """
    Auto-use fixture: restore ``rag_mcp.tools.management._ctx`` after every test.

    ``test_add_pattern.py`` replaces ``management._ctx`` with a local mock in
    each test (``mgmt._ctx = ctx``) but never restores it.  Without this guard,
    any test that runs after one of those tests sees the mock context instead of
    the real server context and gets spurious "project not found" errors.

    This fixture is *autouse* at the package level so it applies to every test
    in the ``mcp_tools/`` package without requiring individual tests to opt in.
    """
    original_ctx = _management._ctx
    yield
    _management._ctx = original_ctx


@pytest.fixture()
def isolated_server_context(monkeypatch):
    """
    Isolate server.config.projects for the duration of one test.

    Replaces ``server.config.projects`` with a shallow copy before the test
    runs; ``monkeypatch`` restores the original list unconditionally after the
    test finishes (pass, fail, or error).

    Any ``server.store.*`` or ``server.loader.*`` method that a test needs to
    stub out should also be patched via this same ``monkeypatch`` instance so
    that every side-effect is undone automatically — for example::

        def test_foo(isolated_server_context, monkeypatch):
            monkeypatch.setattr(server.store, "delete_collection", mock_del)
            monkeypatch.setattr(server.loader, "save", mock_save)
            ...
    """
    monkeypatch.setattr(server.config, "projects", list(server.config.projects))
