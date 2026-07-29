"""
Release version gating helpers for the PyPI release workflow.

Standard-library-only logic used by .github/workflows/release.yml to check that
a pushed Version_Tag (`v<version>`) is consistent with the version declared in
pyproject.toml, that both are valid Semantic Versioning 2.0.0 strings, and that
the release moves the version forward relative to the latest version published
on PyPI.

This module can be imported by tests, and also runs as a CLI step from the
workflow (see `main`), which resolves the latest published version by querying
PyPI's JSON API before delegating to `check_release`.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "ReleaseVersionError",
    "parse_tag_version",
    "is_valid_semver",
    "compare_semver",
    "check_release",
    "fetch_latest_published_version",
    "main",
]

# Default PyPI distribution name looked up by fetch_latest_published_version.
DEFAULT_PACKAGE_NAME = "pow-rag-mcp"

# PyPI's per-project JSON API, per https://warehouse.pypa.io/api-reference/json.html.
_PYPI_JSON_URL_TEMPLATE = "https://pypi.org/pypi/{package_name}/json"


class ReleaseVersionError(Exception):
    """Raised when a release's version tag fails one of the release-gating checks.

    The message names the specific failing check so the workflow log points
    directly at the cause (Requirements 4.4, 4.5, 4.6).
    """

# Official SemVer 2.0.0 grammar, per the regex published at https://semver.org:
#   MAJOR.MINOR.PATCH  - numeric identifiers, no leading zeros
#   -<pre-release>     - optional, dot-separated identifiers; numeric identifiers
#                        must not have leading zeros, alphanumeric identifiers may
#                        contain hyphens, and empty identifiers are invalid
#   +<build>           - optional, dot-separated alphanumeric/hyphen identifiers;
#                        leading zeros allowed, empty identifiers are invalid
#
# re.ASCII keeps \d restricted to 0-9 so non-ASCII digits are rejected.
_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?",
    re.ASCII,
)

TAG_PREFIX = "v"


def parse_tag_version(tag: str) -> str:
    """Strip the leading 'v' from a Version_Tag and return the version string.

    Args:
        tag: A Version_Tag such as ``"v1.2.3"``.

    Returns:
        The tag with its leading ``"v"`` removed, e.g. ``"1.2.3"``.

    Raises:
        ValueError: If ``tag`` does not start with ``"v"``.
    """
    if not tag.startswith(TAG_PREFIX):
        raise ValueError(
            f"Version tag must start with '{TAG_PREFIX}' (expected 'v<version>'), got: {tag!r}"
        )
    return tag[len(TAG_PREFIX):]


def is_valid_semver(version: str) -> bool:
    """Return True iff ``version`` conforms to the SemVer 2.0.0 grammar.

    Args:
        version: The version string to validate, e.g. ``"1.2.3-rc.1+build.5"``.

    Returns:
        True if ``version`` is a valid Semantic Versioning 2.0.0 string,
        False otherwise.
    """
    if not isinstance(version, str):
        return False
    return _SEMVER_RE.fullmatch(version) is not None


def _compare(left: Any, right: Any) -> int:
    """Return -1, 0 or 1 for two directly comparable values of the same type."""
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def _split_version(version: str) -> tuple[tuple[int, int, int], list[str]]:
    """Split a valid SemVer string into its numeric core and pre-release segments.

    Build metadata is discarded: it takes no part in precedence (SemVer 2.0.0 §10).

    Args:
        version: A version string already known to be valid SemVer.

    Returns:
        A ``((major, minor, patch), prerelease_identifiers)`` pair, where the
        identifier list is empty for a version without a pre-release.
    """
    match = _SEMVER_RE.fullmatch(version)
    assert match is not None  # guarded by is_valid_semver in compare_semver
    core = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    prerelease = match.group("prerelease")
    identifiers = prerelease.split(".") if prerelease is not None else []
    return core, identifiers


def _compare_prerelease_identifier(left: str, right: str) -> int:
    """Compare two pre-release identifiers per SemVer 2.0.0 §11.4.

    Identifiers consisting of only digits are compared numerically; identifiers
    with letters or hyphens are compared lexically in ASCII sort order; a numeric
    identifier always has lower precedence than an alphanumeric one.
    """
    left_numeric = left.isdigit()
    right_numeric = right.isdigit()
    if left_numeric and right_numeric:
        return _compare(int(left), int(right))
    if left_numeric != right_numeric:
        # Numeric identifiers always have lower precedence than alphanumeric ones.
        return -1 if left_numeric else 1
    return _compare(left, right)


def compare_semver(a: str, b: str) -> int:
    """Compare two SemVer 2.0.0 strings by precedence.

    Precedence is determined by comparing MAJOR, MINOR and PATCH numerically, in
    that order. When those are equal, a version with a pre-release has *lower*
    precedence than one without; two pre-releases are compared identifier by
    identifier (see :func:`_compare_prerelease_identifier`), and if one runs out
    of identifiers first while all preceding identifiers are equal, the shorter
    one has lower precedence. Build metadata is ignored entirely.

    Args:
        a: The first version string, e.g. ``"1.2.3-rc.1"``.
        b: The second version string, e.g. ``"1.2.3"``.

    Returns:
        -1 if ``a`` has lower precedence than ``b``, 1 if it has higher
        precedence, and 0 if they have equal precedence (which, because build
        metadata is ignored, does not imply the strings are equal).

    Raises:
        ValueError: If either ``a`` or ``b`` is not a valid SemVer 2.0.0 string.
    """
    for name, version in (("a", a), ("b", b)):
        if not is_valid_semver(version):
            raise ValueError(
                f"Argument {name} is not a valid Semantic Versioning 2.0.0 "
                f"string: {version!r}"
            )

    a_core, a_prerelease = _split_version(a)
    b_core, b_prerelease = _split_version(b)

    core_result = _compare(a_core, b_core)
    if core_result != 0:
        return core_result

    if not a_prerelease and not b_prerelease:
        return 0
    if not a_prerelease:
        # No pre-release outranks any pre-release of the same core version.
        return 1
    if not b_prerelease:
        return -1

    for left, right in zip(a_prerelease, b_prerelease):
        identifier_result = _compare_prerelease_identifier(left, right)
        if identifier_result != 0:
            return identifier_result

    # All shared identifiers are equal: the longer identifier list wins.
    return _compare(len(a_prerelease), len(b_prerelease))


def check_release(
    tag: str,
    pyproject_version: str,
    latest_published_version: str | None = None,
) -> None:
    """Run every release-gating version check, in order, for one release.

    The checks run sequentially and the first failure raises immediately, so the
    error surfaced to the workflow always names the earliest problem rather than a
    downstream symptom of it:

    1. ``tag`` has the Version_Tag form ``v<version>`` (Requirement 4.3).
    2. The version derived from ``tag`` is valid SemVer 2.0.0 (Requirement 4.3).
    3. ``pyproject_version`` is valid SemVer 2.0.0 (Requirement 4.5).
    4. The tag-derived version equals ``pyproject_version`` as an exact string,
       with no normalization or numeric tolerance (Requirement 4.4).
    5. When ``latest_published_version`` is given, the tag-derived version has
       strictly greater SemVer precedence than it (Requirement 4.6). A
       ``latest_published_version`` of ``None`` means no prior release exists
       (first-ever publish), and this check is skipped.

    Args:
        tag: The pushed Version_Tag, e.g. ``"v1.2.3"``.
        pyproject_version: The ``project.version`` value from pyproject.toml.
        latest_published_version: The most recently published PyPI version, or
            None if the project has never been published.

    Returns:
        None. Returning normally means every check passed and the release may
        proceed.

    Raises:
        ReleaseVersionError: On the first failing check, with a message naming
            that check.
    """
    try:
        tag_version = parse_tag_version(tag)
    except ValueError as exc:
        raise ReleaseVersionError(
            f"Version tag {tag!r} does not match the required "
            f"'{TAG_PREFIX}<version>' pattern: {exc}"
        ) from exc

    if not is_valid_semver(tag_version):
        raise ReleaseVersionError(
            f"Version {tag_version!r} derived from tag {tag!r} is not a valid "
            f"Semantic Versioning 2.0.0 string"
        )

    if not is_valid_semver(pyproject_version):
        raise ReleaseVersionError(
            f"pyproject.toml project.version {pyproject_version!r} is not a valid "
            f"Semantic Versioning 2.0.0 string"
        )

    if tag_version != pyproject_version:
        raise ReleaseVersionError(
            f"Version {tag_version!r} derived from tag {tag!r} does not match "
            f"pyproject.toml project.version {pyproject_version!r}"
        )

    if latest_published_version is None:
        return

    if not is_valid_semver(latest_published_version):
        raise ReleaseVersionError(
            f"Latest published version {latest_published_version!r} is not a valid "
            f"Semantic Versioning 2.0.0 string"
        )

    if compare_semver(tag_version, latest_published_version) <= 0:
        raise ReleaseVersionError(
            f"Version {tag_version!r} derived from tag {tag!r} is not greater than "
            f"the latest published version {latest_published_version!r} per "
            f"Semantic Versioning precedence rules"
        )


def fetch_latest_published_version(
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> str | None:
    """Look up the latest version of ``package_name`` published on PyPI.

    Queries PyPI's JSON API for the project. A 404 response means the project
    has never been published, which is reported as "no prior version" rather
    than as an error so the caller can skip the precedence gate for a
    first-ever release.

    Args:
        package_name: The PyPI distribution name to look up, e.g.
            ``"pow-rag-mcp"``.

    Returns:
        The value of ``info.version`` from the PyPI JSON API response, or None
        if the project does not exist on PyPI yet (HTTP 404).

    Raises:
        RuntimeError: If the PyPI API returns a non-2xx, non-404 status, if the
            request fails at the network level, or if the response body is not
            valid JSON or is missing the expected ``info.version`` field.
    """
    url = _PYPI_JSON_URL_TEMPLATE.format(package_name=package_name)
    try:
        with urllib.request.urlopen(url) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(
            f"PyPI JSON API request to {url!r} failed with HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"PyPI JSON API request to {url!r} failed: {exc.reason}"
        ) from exc

    if status != 200:
        raise RuntimeError(
            f"PyPI JSON API request to {url!r} returned unexpected HTTP status {status}"
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"PyPI JSON API response from {url!r} was not valid JSON: {exc}"
        ) from exc

    try:
        return payload["info"]["version"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"PyPI JSON API response from {url!r} is missing the 'info.version' field"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: check a release's tag/version against PyPI, and exit.

    Resolves ``latest_published_version`` by querying PyPI's JSON API for
    ``package_name`` (a 404 is treated as "no prior version" and skips the
    precedence gate), then delegates to :func:`check_release`. Intended to be
    invoked as a workflow step, e.g.::

        python scripts/check_release_version.py "$GITHUB_REF_NAME" "1.2.3"

    Args:
        argv: Command-line arguments to parse, excluding the program name.
            Defaults to ``sys.argv[1:]`` when None.

    Returns:
        0 if every release-gating check passed, 1 if the PyPI lookup or
        `check_release` failed. The specific failing reason is printed to
        stderr in the failure case rather than raised as an unhandled
        exception.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Check that a pushed Version_Tag is consistent with pyproject.toml's "
            "version, is valid SemVer, and moves the version forward relative to "
            "the latest version published on PyPI."
        )
    )
    parser.add_argument("tag", help="The pushed Version_Tag, e.g. 'v1.2.3'.")
    parser.add_argument(
        "pyproject_version",
        help="The project.version value from pyproject.toml.",
    )
    parser.add_argument(
        "--package-name",
        default=DEFAULT_PACKAGE_NAME,
        help=(
            "PyPI distribution name to look up the latest published version for "
            f"(default: {DEFAULT_PACKAGE_NAME!r})."
        ),
    )
    args = parser.parse_args(argv)

    try:
        latest_published_version = fetch_latest_published_version(args.package_name)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        check_release(args.tag, args.pyproject_version, latest_published_version)
    except ReleaseVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
