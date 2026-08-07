"""Standalone validator for all JSON case packs under tests/cases."""

from __future__ import annotations

import sys

from tests.runners.casepacks import validate_all_casepacks


def main() -> int:
    errors = validate_all_casepacks()
    if not errors:
        print("Case pack validation passed.")
        return 0

    print("Case pack validation failed:")
    for error in errors:
        print(f"- {error.format()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

