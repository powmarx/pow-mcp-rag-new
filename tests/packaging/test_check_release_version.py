"""Property-based tests for scripts/check_release_version.py.

Covers the correctness properties defined in
.kiro/specs/pypi-package-publishing/design.md.
"""

import string
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).parent.parent.parent
assert (REPO_ROOT / "pyproject.toml").exists(), (
    f"REPO_ROOT did not resolve to the repo root: {REPO_ROOT}"
)
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_release_version import (
    ReleaseVersionError,
    check_release,
    compare_semver,
    is_valid_semver,
    parse_tag_version,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Identifier character set shared by pre-release and build-metadata identifiers.
_ID_CHARS = string.digits + string.ascii_letters + "-"

# Numeric identifier: 0 | [1-9]\d*  — rendering an int never produces a leading zero.
_numeric_identifier = st.integers(min_value=0, max_value=10**6).map(str)

# Alphanumeric identifier: \d*[a-zA-Z-][0-9a-zA-Z-]* — at least one non-digit,
# built as (optional digits) + (one letter/hyphen) + (arbitrary id chars).
_alphanumeric_identifier = st.builds(
    lambda digits, non_digit, rest: digits + non_digit + rest,
    st.text(alphabet=string.digits, max_size=3),
    st.sampled_from(string.ascii_letters + "-"),
    st.text(alphabet=_ID_CHARS, max_size=5),
)

_prerelease = st.lists(
    st.one_of(_numeric_identifier, _alphanumeric_identifier),
    min_size=1,
    max_size=3,
).map(".".join)

# Build identifiers allow leading zeros and pure digits, but not empty segments.
_build_metadata = st.lists(
    st.text(alphabet=_ID_CHARS, min_size=1, max_size=5),
    min_size=1,
    max_size=3,
).map(".".join)


@st.composite
def valid_semver(draw):
    """Generate grammar-conformant SemVer 2.0.0 strings.

    MAJOR.MINOR.PATCH built from non-negative ints (so no leading zeros), with an
    optional dot-separated pre-release and an optional dot-separated build metadata
    suffix.
    """
    core = ".".join(
        str(draw(st.integers(min_value=0, max_value=10**6))) for _ in range(3)
    )
    prerelease = draw(st.none() | _prerelease)
    build = draw(st.none() | _build_metadata)
    version = core
    if prerelease is not None:
        version += f"-{prerelease}"
    if build is not None:
        version += f"+{build}"
    return version


def _mutate_core_component(version, index, replacement):
    """Replace the MAJOR/MINOR/PATCH component at `index` with `replacement`."""
    core, sep, rest = _split_core(version)
    parts = core.split(".")
    parts[index] = replacement
    return ".".join(parts) + sep + rest


def _split_core(version):
    """Split `version` into (core, separator, remainder) at the first '-' or '+'."""
    for i, char in enumerate(version):
        if char in "-+":
            return version[:i], char, version[i + 1:]
    return version, "", ""


@st.composite
def grammar_violating_version(draw):
    """Generate strings that mutate valid SemVer in grammar-relevant ways.

    Mutations target each documented failure mode: leading zeros, missing or extra
    core components, non-numeric core components, and empty pre-release/build
    identifiers. A mutation may occasionally still yield a valid string (e.g.
    replacing "1" with "2"); the reference oracle, not the generator, decides
    validity, so that is harmless.
    """
    version = draw(valid_semver())
    index = draw(st.integers(min_value=0, max_value=2))
    mutation = draw(
        st.sampled_from(
            [
                "leading_zero",
                "drop_component",
                "extra_component",
                "non_numeric_component",
                "empty_component",
                "empty_prerelease_identifier",
                "empty_prerelease",
                "empty_build",
                "tag_prefix",
                "surrounding_whitespace",
                "non_ascii_digit",
            ]
        )
    )
    core, sep, rest = _split_core(version)

    if mutation == "leading_zero":
        parts = core.split(".")
        return _mutate_core_component(version, index, "0" + parts[index])
    if mutation == "drop_component":
        parts = core.split(".")
        del parts[index]
        return ".".join(parts) + sep + rest
    if mutation == "extra_component":
        extra = draw(_numeric_identifier)
        return core + "." + extra + sep + rest
    if mutation == "non_numeric_component":
        return _mutate_core_component(version, index, draw(_alphanumeric_identifier))
    if mutation == "empty_component":
        return _mutate_core_component(version, index, "")
    if mutation == "empty_prerelease_identifier":
        prerelease = draw(_prerelease)
        position = draw(st.sampled_from(["leading", "middle", "trailing"]))
        if position == "leading":
            prerelease = "." + prerelease
        elif position == "trailing":
            prerelease = prerelease + "."
        else:
            prerelease = prerelease + ".." + draw(_numeric_identifier)
        return f"{core}-{prerelease}"
    if mutation == "empty_prerelease":
        return f"{core}-"
    if mutation == "empty_build":
        return f"{version}+" if "+" not in version else f"{core}+"
    if mutation == "tag_prefix":
        return "v" + version
    if mutation == "surrounding_whitespace":
        return draw(st.sampled_from([" ", "\n", "\t"])) + version
    # non_ascii_digit
    return _mutate_core_component(version, index, "\uff11")


def arbitrary_version_like():
    """Mix valid SemVer, grammar-violating mutations, and free-form junk strings."""
    return st.one_of(
        valid_semver(),
        grammar_violating_version(),
        st.text(alphabet=_ID_CHARS + ".+", max_size=12),
        st.text(max_size=8),
    )


# Pre-release identifiers drawn from a small pool so independent draws collide
# often: that is what exercises the numeric-vs-alphanumeric, per-segment and
# identifier-count branches of precedence comparison.
_PRERELEASE_IDENTIFIER_POOL = [
    "0",
    "1",
    "2",
    "9",
    "10",
    "alpha",
    "beta",
    "rc",
    "-",
    "0a",
    "1alpha",
    "alpha1",
    "Alpha",
]

_small_core = st.builds(
    lambda major, minor, patch: f"{major}.{minor}.{patch}",
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
)

_small_prerelease = st.lists(
    st.sampled_from(_PRERELEASE_IDENTIFIER_POOL), min_size=1, max_size=3
).map(".".join)


@st.composite
def colliding_semver(draw):
    """Generate valid SemVer strings from a deliberately tiny value pool.

    Cores come from 0-2 in each position and pre-release identifiers from a fixed
    pool, so two independent draws frequently share a core and/or a pre-release
    prefix. That makes the pre-release and build-metadata comparison paths (which
    are only reached when the cores tie) actually get hit.
    """
    version = draw(_small_core)
    prerelease = draw(st.none() | _small_prerelease)
    build = draw(st.none() | _build_metadata)
    if prerelease is not None:
        version += f"-{prerelease}"
    if build is not None:
        version += f"+{build}"
    return version


def precedence_candidate():
    """Mix broad valid SemVer with near-colliding versions for ordering tests."""
    return st.one_of(valid_semver(), colliding_semver())


# ---------------------------------------------------------------------------
# Reference oracle: an independent, hand-rolled implementation of the
# SemVer 2.0.0 grammar (deliberately not the regex used by the module).
# ---------------------------------------------------------------------------


def _is_numeric_identifier(segment):
    """Numeric identifier: 0 | [1-9]\\d* (ASCII digits only, no leading zeros)."""
    if not segment:
        return False
    if any(char not in string.digits for char in segment):
        return False
    return not (len(segment) > 1 and segment[0] == "0")


def _is_alphanumeric_identifier(segment):
    """Alphanumeric identifier: [0-9a-zA-Z-]+ containing at least one non-digit."""
    if not segment:
        return False
    if any(char not in _ID_CHARS for char in segment):
        return False
    return any(char not in string.digits for char in segment)


def matches_semver_grammar_reference(value):
    """Return True iff `value` satisfies the SemVer 2.0.0 grammar.

    Hand-rolled reference implementation used as the oracle for Property 2:
    splits build metadata at the first '+', then the pre-release at the first '-',
    then validates the three remaining dot-separated numeric core identifiers.
    """
    if not isinstance(value, str):
        return False

    plus = value.find("+")
    if plus != -1:
        build = value[plus + 1:]
        value = value[:plus]
        if not build:
            return False
        if not all(
            segment and all(char in _ID_CHARS for char in segment)
            for segment in build.split(".")
        ):
            return False

    hyphen = value.find("-")
    if hyphen != -1:
        prerelease = value[hyphen + 1:]
        value = value[:hyphen]
        if not prerelease:
            return False
        if not all(
            _is_numeric_identifier(segment) or _is_alphanumeric_identifier(segment)
            for segment in prerelease.split(".")
        ):
            return False

    core = value.split(".")
    if len(core) != 3:
        return False
    return all(_is_numeric_identifier(segment) for segment in core)


def _prerelease_identifier_key(identifier):
    """Sort key for one pre-release identifier (SemVer 2.0.0 §11.4).

    Numeric identifiers rank below alphanumeric ones, numeric identifiers compare
    numerically among themselves, and alphanumeric identifiers compare lexically
    in ASCII order.
    """
    if all(char in string.digits for char in identifier):
        return (0, int(identifier), "")
    return (1, 0, identifier)


def semver_precedence_key_reference(version):
    """Return a sort key whose natural ordering is SemVer 2.0.0 precedence.

    Independent reference used as the oracle for Property 4: build metadata is
    dropped, the numeric core compares as an int triple, a version without a
    pre-release ranks above the same core with one (flag 1 > flag 0), and
    pre-release identifier lists compare lexicographically, which also gives the
    "shorter list has lower precedence" rule for free.
    """
    plus = version.find("+")
    if plus != -1:
        version = version[:plus]

    hyphen = version.find("-")
    if hyphen != -1:
        prerelease = version[hyphen + 1:]
        version = version[:hyphen]
    else:
        prerelease = None

    core = tuple(int(segment) for segment in version.split("."))
    if prerelease is None:
        return (core, 1, [])
    return (
        core,
        0,
        [_prerelease_identifier_key(identifier) for identifier in prerelease.split(".")],
    )


def _sign(value):
    """Return -1, 0 or 1 matching the sign of `value`."""
    return (value > 0) - (value < 0)


# Substrings unique to check_release's tag/pyproject match error message, used to
# confirm a raised ReleaseVersionError came from the match gate and not from one
# of the surrounding gates.
_MATCH_ERROR_MARKERS = ("does not match", "pyproject.toml project.version")


def version_match_gate_raises(tag_version, pyproject_version):
    """Run only check_release's tag/pyproject match gate and report whether it failed.

    Isolation strategy: both arguments are valid SemVer strings, so the tag-format
    gate (a 'v' prefix is added here), the tag SemVer gate and the pyproject SemVer
    gate all pass; passing latest_published_version=None skips the precedence gate.
    The exact-match check is therefore the only gate that can fail, and the raised
    message is asserted to be the match-specific one so a failure from any other
    gate surfaces as a test error rather than being counted as a match rejection.

    Returns:
        True if the match gate raised ReleaseVersionError, False if check_release
        returned normally.
    """
    try:
        check_release(
            "v" + tag_version, pyproject_version, latest_published_version=None
        )
    except ReleaseVersionError as exc:
        message = str(exc)
        assert all(marker in message for marker in _MATCH_ERROR_MARKERS), (
            f"expected the tag/pyproject match error, got: {message!r}"
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

# Feature: pypi-package-publishing, Property 1: Tag version stripping round-trip
@settings(max_examples=100)
@given(valid_semver())
def test_tag_stripping_round_trip(version):
    """For any valid SemVer string V, parse_tag_version("v" + V) returns exactly V.

    **Validates: Requirements 4.3**
    """
    assert parse_tag_version("v" + version) == version


# Feature: pypi-package-publishing, Property 2: SemVer validity classification is exact
@settings(max_examples=100)
@given(arbitrary_version_like())
def test_semver_validity_is_exact(value):
    """is_valid_semver(S) is True iff S conforms to the SemVer 2.0.0 grammar.

    Holds for strings generated to be valid SemVer, for strings generated to
    violate the grammar (leading zeros, missing components, non-numeric
    major/minor/patch, empty identifiers), and for free-form junk.

    **Validates: Requirements 4.1, 4.3, 4.5**
    """
    assert is_valid_semver(value) == matches_semver_grammar_reference(value)


# Feature: pypi-package-publishing, Property 3: Version match gate is exact-equality
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(precedence_candidate(), precedence_candidate())
def test_version_match_gate(a, b):
    """check_release's match gate rejects iff the two version strings differ exactly.

    The gate raises ReleaseVersionError if and only if A != B as strings, with no
    normalization and no numeric tolerance (so versions differing only in build
    metadata, which have equal SemVer precedence, are still rejected). Checking any
    version against itself always passes.

    **Validates: Requirements 4.4**
    """
    assert version_match_gate_raises(a, b) == (a != b)
    assert version_match_gate_raises(a, a) is False
    assert version_match_gate_raises(b, b) is False


# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(precedence_candidate(), precedence_candidate(), precedence_candidate())
def test_semver_precedence_consistency(a, b, c):
    """compare_semver is antisymmetric and transitive over valid SemVer strings.

    compare_semver(A, B) == -compare_semver(B, A) for every pair, and if
    compare_semver(A, B) <= 0 and compare_semver(B, C) <= 0 then
    compare_semver(A, C) <= 0. Together with reflexivity these make the relation a
    consistent total preorder, which is what the release gate relies on.

    **Validates: Requirements 4.6**
    """
    assert compare_semver(a, b) == -compare_semver(b, a)
    assert compare_semver(a, a) == 0
    if compare_semver(a, b) <= 0 and compare_semver(b, c) <= 0:
        assert compare_semver(a, c) <= 0


# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(precedence_candidate(), precedence_candidate())
def test_semver_precedence_matches_reference_ordering(a, b):
    """compare_semver agrees with an independent SemVer 2.0.0 precedence oracle.

    The oracle keys each version by (core triple, has-no-pre-release flag,
    per-identifier keys), so this covers numeric core ordering, build metadata
    being ignored, release outranking pre-release, and per-segment pre-release
    comparison including numeric-below-alphanumeric.

    **Validates: Requirements 4.6**
    """
    key_a = semver_precedence_key_reference(a)
    key_b = semver_precedence_key_reference(b)
    expected = (key_a > key_b) - (key_a < key_b)
    assert _sign(compare_semver(a, b)) == expected


# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(_small_core, st.none() | _small_prerelease, _build_metadata, _build_metadata)
def test_semver_build_metadata_is_ignored(core, prerelease, build_a, build_b):
    """Build metadata takes no part in precedence (SemVer 2.0.0 §10).

    Two versions differing only in build metadata, and the same version with and
    without build metadata, all have equal precedence.

    **Validates: Requirements 4.6**
    """
    base = core if prerelease is None else f"{core}-{prerelease}"
    assert compare_semver(f"{base}+{build_a}", f"{base}+{build_b}") == 0
    assert compare_semver(base, f"{base}+{build_a}") == 0


# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
@settings(max_examples=100)
@given(_small_core, _small_prerelease, _small_prerelease)
def test_prerelease_has_lower_precedence_than_release(core, prerelease, extra):
    """A pre-release ranks below the same MAJOR.MINOR.PATCH without one.

    Also checks that appending an identifier to a pre-release raises its
    precedence, i.e. a shorter identifier list is lower when every shared
    identifier is equal.

    **Validates: Requirements 4.6**
    """
    assert compare_semver(f"{core}-{prerelease}", core) == -1
    assert compare_semver(core, f"{core}-{prerelease}") == 1
    assert compare_semver(f"{core}-{prerelease}", f"{core}-{prerelease}.{extra}") == -1


# ---------------------------------------------------------------------------
# Example-based unit tests
# ---------------------------------------------------------------------------

# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
def test_semver_spec_worked_precedence_example():
    """compare_semver reproduces the SemVer 2.0.0 spec's own worked example.

    The chain 1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta <
    1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0 is given verbatim in the
    SemVer 2.0.0 spec (§11) as an example of precedence ordering.

    **Validates: Requirements 4.6**
    """
    chain = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    for lower, higher in zip(chain, chain[1:]):
        assert compare_semver(lower, higher) == -1, f"{lower!r} vs {higher!r}"
        assert compare_semver(higher, lower) == 1, f"{higher!r} vs {lower!r}"


# Feature: pypi-package-publishing, Property 1: Tag version stripping round-trip
def test_parse_tag_version_missing_v_prefix_raises():
    """parse_tag_version raises ValueError for a tag with no leading 'v'.

    **Validates: Requirements 4.3**
    """
    try:
        parse_tag_version("1.2.3")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a tag with no leading 'v'")


# Feature: pypi-package-publishing, Property 4: SemVer precedence ordering and publish gate
def test_check_release_first_ever_release_skips_precedence_gate():
    """check_release skips the precedence gate when latest_published_version is None.

    A first-ever release has no prior published version to compare against, so
    passing latest_published_version=None must succeed as long as the tag-format,
    SemVer-validity and tag/pyproject match gates all pass, with no precedence
    comparison performed.

    **Validates: Requirements 4.3, 4.4, 4.5, 4.6**
    """
    check_release("v1.0.0", "1.0.0", latest_published_version=None)
