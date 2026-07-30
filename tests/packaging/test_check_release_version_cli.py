"""Unit tests for the CLI wrapper (`main`) in scripts/check_release_version.py.

Covers Requirements 4.6 and 5.3: resolving `latest_published_version` from PyPI's
JSON API and surfacing the result as a CLI exit code / stderr message. The PyPI
HTTP call is mocked via `urllib.request.urlopen` so these tests run offline and
deterministically.
"""

import json
import sys
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
assert (REPO_ROOT / "pyproject.toml").exists(), (
    f"REPO_ROOT did not resolve to the repo root: {REPO_ROOT}"
)
sys.path.insert(0, str(REPO_ROOT))

from scripts import check_release_version


class _FakeHTTPResponse:
    """Minimal stand-in for the object returned by `urllib.request.urlopen`."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def _make_404_error(url):
    return urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)


def _make_500_error(url):
    return urllib.error.HTTPError(url, 500, "Internal Server Error", hdrs=None, fp=None)


# Feature: pypi-package-publishing
def test_main_successful_invocation_returns_zero(monkeypatch, capsys):
    """main() exits 0 and prints nothing to stderr on a successful release check.

    The PyPI lookup succeeds and returns a lower-precedence prior version, so the
    tag/pyproject match and precedence gates in `check_release` both pass.

    **Validates: Requirements 4.6, 5.3**
    """
    body = json.dumps({"info": {"version": "1.2.3"}}).encode("utf-8")

    def fake_urlopen(url, *args, **kwargs):
        assert "pow-rag-mcp" in url
        return _FakeHTTPResponse(body, status=200)

    monkeypatch.setattr(check_release_version.urllib.request, "urlopen", fake_urlopen)

    exit_code = check_release_version.main(["v1.2.4", "1.2.4"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


# Feature: pypi-package-publishing
def test_main_404_treated_as_no_prior_version(monkeypatch, capsys):
    """A 404 from the PyPI JSON API is treated as "no prior version" and does not fail the CLI.

    fetch_latest_published_version returns None for a 404, which makes
    check_release skip the precedence gate entirely (first-ever release), so
    main() still exits 0 as long as the tag/pyproject checks pass.

    **Validates: Requirements 4.6, 5.3**
    """

    def fake_urlopen(url, *args, **kwargs):
        raise _make_404_error(url)

    monkeypatch.setattr(check_release_version.urllib.request, "urlopen", fake_urlopen)

    exit_code = check_release_version.main(["v1.0.0", "1.0.0"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


# Feature: pypi-package-publishing
def test_main_non_2xx_non_404_pypi_error_surfaces_as_cli_failure(monkeypatch, capsys):
    """A non-2xx/non-404 PyPI API error surfaces as a CLI failure (exit 1, stderr message).

    fetch_latest_published_version raises RuntimeError for an unexpected HTTP
    status such as 500; main() must catch that specific error, print the reason
    to stderr, and return a non-zero exit code without an unhandled exception.

    **Validates: Requirements 4.6, 5.3**
    """

    def fake_urlopen(url, *args, **kwargs):
        raise _make_500_error(url)

    monkeypatch.setattr(check_release_version.urllib.request, "urlopen", fake_urlopen)

    exit_code = check_release_version.main(["v1.2.4", "1.2.4"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.startswith("error:")
    assert "500" in captured.err


# Feature: pypi-package-publishing
def test_main_pypi_error_does_not_reach_check_release(monkeypatch, capsys):
    """When the PyPI lookup fails, main() reports the lookup error, not a check_release error.

    This confirms the CLI failure path is wired to fetch_latest_published_version's
    RuntimeError specifically, rather than accidentally passing an error sentinel
    through to check_release and getting a ReleaseVersionError message instead.

    **Validates: Requirements 4.6, 5.3**
    """

    def fake_urlopen(url, *args, **kwargs):
        raise _make_500_error(url)

    monkeypatch.setattr(check_release_version.urllib.request, "urlopen", fake_urlopen)

    # Deliberately invalid tag/pyproject_version: if the RuntimeError from the
    # PyPI lookup were swallowed, check_release would fail for a *different*
    # reason and this assertion on the stderr message would catch that.
    exit_code = check_release_version.main(["not-a-tag", "not-a-version"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PyPI JSON API" in captured.err
    assert "does not match" not in captured.err
