"""Loading and validating JSON case packs for data-driven tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CASES_DIR = Path(__file__).resolve().parent.parent / "cases"
SCHEMA_DIR = CASES_DIR / "schema"

ALLOWED_RUNNERS = {
    "validation_runner",
    "state_runner",
    "subprocess_runner",
}

ALLOWED_ASSERTIONS = {
    "contains",
    "not_contains",
    "contains_any",
    "equals",
    "starts_with",
    "not_starts_with",
    "regex",
    "length_equals",
    "length_gte",
    "length_lte",
}


@dataclass(frozen=True)
class CasePackValidationError:
    """Describes one schema/shape validation issue in a case pack file."""

    file_path: Path
    message: str

    def format(self) -> str:
        return f"{self.file_path}: {self.message}"


def load_casepack(file_path: Path) -> dict[str, Any]:
    with file_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_path}: top-level JSON value must be an object")
    return loaded


def iter_casepack_files(cases_dir: Path | None = None) -> list[Path]:
    root = cases_dir or CASES_DIR
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.json") if SCHEMA_DIR not in p.parents)


def validate_all_casepacks(cases_dir: Path | None = None) -> list[CasePackValidationError]:
    errors: list[CasePackValidationError] = []
    for file_path in iter_casepack_files(cases_dir):
        try:
            pack = load_casepack(file_path)
        except Exception as exc:
            errors.append(CasePackValidationError(file_path=file_path, message=str(exc)))
            continue
        errors.extend(validate_casepack(pack, file_path))
    return errors


def load_cases(file_name: str, cases_dir: Path | None = None) -> list[dict[str, Any]]:
    root = cases_dir or CASES_DIR
    file_path = root / file_name
    pack = load_casepack(file_path)
    errors = validate_casepack(pack, file_path)
    if errors:
        formatted = "\n".join(f"- {e.format()}" for e in errors)
        raise ValueError(f"Invalid case pack '{file_name}':\n{formatted}")
    return pack["cases"]


def validate_casepack(pack: dict[str, Any], file_path: Path) -> list[CasePackValidationError]:
    errors: list[CasePackValidationError] = []

    _require_type(pack, "schema", str, file_path, errors)
    _require_type(pack, "pack_id", str, file_path, errors)
    _require_type(pack, "domain", str, file_path, errors)
    _require_type(pack, "version", int, file_path, errors)
    _require_type(pack, "cases", list, file_path, errors)

    cases = pack.get("cases")
    if isinstance(cases, list):
        ids: set[str] = set()
        for idx, case in enumerate(cases):
            prefix = f"cases[{idx}]"
            if not isinstance(case, dict):
                errors.append(
                    CasePackValidationError(file_path, f"{prefix}: case entry must be an object")
                )
                continue

            case_id = case.get("id")
            if not isinstance(case_id, str) or not case_id.strip():
                errors.append(CasePackValidationError(file_path, f"{prefix}.id: must be non-empty string"))
            elif case_id in ids:
                errors.append(CasePackValidationError(file_path, f"{prefix}.id: duplicate id '{case_id}'"))
            else:
                ids.add(case_id)

            runner = case.get("runner")
            if not isinstance(runner, str) or runner not in ALLOWED_RUNNERS:
                allowed = ", ".join(sorted(ALLOWED_RUNNERS))
                errors.append(
                    CasePackValidationError(
                        file_path,
                        f"{prefix}.runner: must be one of [{allowed}]",
                    )
                )

            operation = case.get("operation")
            if not isinstance(operation, str) or "." not in operation:
                errors.append(
                    CasePackValidationError(
                        file_path,
                        f"{prefix}.operation: must be non-empty 'group.action' string",
                    )
                )

            _require_nested_type(case, "inputs", dict, file_path, errors, prefix)

            expect = case.get("expect")
            if expect is not None and not isinstance(expect, dict):
                errors.append(CasePackValidationError(file_path, f"{prefix}.expect: must be an object"))
            if isinstance(expect, dict):
                _validate_expect(file_path, prefix, expect, errors)

            state_assertions = case.get("state_assertions")
            if state_assertions is not None and not isinstance(state_assertions, list):
                errors.append(
                    CasePackValidationError(file_path, f"{prefix}.state_assertions: must be an array")
                )

    return errors


def _validate_expect(
    file_path: Path,
    prefix: str,
    expect: dict[str, Any],
    errors: list[CasePackValidationError],
) -> None:
    for key, value in expect.items():
        if key not in ALLOWED_ASSERTIONS:
            allowed = ", ".join(sorted(ALLOWED_ASSERTIONS))
            errors.append(
                CasePackValidationError(
                    file_path,
                    f"{prefix}.expect.{key}: unsupported assertion operator; allowed [{allowed}]",
                )
            )
            continue

        if key in {"contains", "not_contains", "contains_any"}:
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                errors.append(
                    CasePackValidationError(file_path, f"{prefix}.expect.{key}: must be an array of strings")
                )
        elif key in {"equals", "starts_with", "not_starts_with", "regex"}:
            if not isinstance(value, str):
                errors.append(
                    CasePackValidationError(file_path, f"{prefix}.expect.{key}: must be a string")
                )
        elif key in {"length_equals", "length_gte", "length_lte"}:
            if not isinstance(value, int):
                errors.append(
                    CasePackValidationError(file_path, f"{prefix}.expect.{key}: must be an integer")
                )


def _require_type(
    source: dict[str, Any],
    key: str,
    expected_type: type,
    file_path: Path,
    errors: list[CasePackValidationError],
) -> None:
    value = source.get(key)
    if not isinstance(value, expected_type):
        errors.append(CasePackValidationError(file_path, f"{key}: must be {expected_type.__name__}"))


def _require_nested_type(
    source: dict[str, Any],
    key: str,
    expected_type: type,
    file_path: Path,
    errors: list[CasePackValidationError],
    prefix: str,
) -> None:
    value = source.get(key)
    if not isinstance(value, expected_type):
        errors.append(CasePackValidationError(file_path, f"{prefix}.{key}: must be {expected_type.__name__}"))
