"""Runner for subprocess-oriented JSON cases."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from tests.runners.assertions import assert_expected_text
from tests.runners.templating import resolve_case


REPO_ROOT = Path(__file__).parent.parent.parent


def run_subprocess_case(case: dict[str, Any], context: dict[str, Any] | None = None) -> None:
    rendered_case = resolve_case(case, context)
    inputs = rendered_case.get("inputs", {})
    command = inputs.get("command")
    if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
        raise AssertionError("subprocess_runner expects inputs.command as list[str]")

    env = dict(os.environ)
    env_overrides = inputs.get("env", {})
    if isinstance(env_overrides, dict):
        env.update({k: str(v) for k, v in env_overrides.items()})

    timeout_seconds = inputs.get("timeout_seconds")
    expect_timeout = bool(inputs.get("expect_timeout", False))
    allow_timeout = bool(inputs.get("allow_timeout", False))
    timed_out = False

    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=float(timeout_seconds) if timeout_seconds is not None else None,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = (exc.stdout or "") + (exc.stderr or "")
        completed = None

    if expect_timeout:
        assert timed_out, (
            f"Expected subprocess timeout for command {command}, but it completed.\nOutput:\n{output}"
        )
    elif timed_out and not allow_timeout:
        raise AssertionError(
            f"Subprocess timed out unexpectedly for command {command}.\nOutput:\n{output}"
        )

    expected_exit = inputs.get("expected_exit_code")
    if expected_exit is not None:
        if completed is not None:
            assert completed.returncode == int(expected_exit), (
                f"Expected exit {expected_exit}, got {completed.returncode}\nOutput:\n{output}"
            )

    assert_expected_text(output, rendered_case.get("expect", {}))
