# Requirements Document

## Introduction

The `pow-rag-mcp` project's pytest suite (`tests/`) is currently excluded from git via `.gitignore`, so no test file has ever been committed. This means the GitHub Actions release workflow (`.github/workflows/release.yml`, job `test`) collects zero tests on a fresh checkout and fails with pytest exit code 5 ("no tests ran"), blocking the entire release pipeline before it reaches the build/publish stages.

Separately, the ~19 test files that exist locally (but untracked) are organized as a flat list directly under `tests/` with no goal-based grouping, and many test functions repeat near-identical assertions across hardcoded individual cases rather than using parametrization.

Finally, running the current suite locally produces 20 failing tests out of 176 (156 passing, 3 skipped). Investigation during requirements gathering confirmed these failures fall into four distinct root-cause categories:

1. **Hardcoded, machine-specific fixtures** — `test_add_file_folder.py` and most of `test_mcp_tools.py` hardcode Windows developer-machine paths (e.g. `C:/Users/you/GIT/my-project/...`) and assume a pre-existing indexed ChromaDB project (`my-project`, `my-project-a`) that does not exist on a fresh checkout or in CI.
2. **A genuine product bug** — `ProjectAutoDetector._apply_stack_rules` in `src/rag_mcp/auto_detector.py` does not correctly evaluate the `has_direct_files` / `patterns_if_direct` / `patterns_if_nested` branch against `config/detection_rules.json`.
3. **Shared mutable global state across tests** — `test_clear_project_index.py` and `test_remove_project.py` mutate a module-level `server.config` / `server.store` singleton without adequate isolation between tests, causing order-dependent failures (e.g. a `removed` flag not reflecting the change just made, or a project inserted by one test appearing "not found" in another).
4. **A flaky hardcoded timing threshold** — `test_mcp_connection.py` asserts server startup completes in under 30 seconds; measured startup was 32 seconds, an environment-dependent, non-deterministic bound rather than a real regression.

Note: tracebacks in the originally reported failure output referenced a path (`D:\GitHub\tools-mcp-rag`) different from this repository's checkout path. Investigation confirmed this is leftover `__pycache__` / `.pytest_cache` bytecode from before the local working copy was renamed/relocated — not contamination from a separate sibling repository. Clearing `__pycache__` and re-running reproduces the same 20 failures from this repository's own files, confirming they are real, in-repo issues to fix, not environment contamination to merely flag.

This feature un-ignores and commits `tests/` to version control, reorganizes it into goal-based folders, converts appropriate repetitive tests to a data-driven/parametrized style, and fixes the 20 pre-existing failures — all while keeping the suite runnable unattended on a fresh Linux checkout in GitHub Actions with no local indexed ChromaDB data.

## Glossary

- **Test_Suite**: The collection of pytest test files and fixtures under the `tests/` directory of this repository.
- **Repository**: The git repository for the `pow-rag-mcp` (package `rag_mcp`) project.
- **CI_Workflow**: The GitHub Actions job named `test` in `.github/workflows/release.yml`, which runs `pip install -e ".[dev]"` followed by `pytest` on a fresh checkout.
- **Test_Folder_Structure**: The goal-based directory layout under `tests/` that groups test files by the feature area they exercise, replacing the current flat layout.
- **Data_Driven_Test**: A test implemented using `pytest.mark.parametrize` (or an equivalent table-driven data structure) so that multiple input/expected-output cases are expressed as data rather than as separate, near-duplicate test functions.
- **Failing_Test**: One of the 20 tests, identified in the Introduction, that fails when the Test_Suite is run against the current codebase before this feature's changes.
- **Environment_Fixture**: Test setup data (file paths, project names, indexed ChromaDB collections) that a test depends on existing in the execution environment.
- **Shared_Server_Context**: The module-level `config`, `store`, and `loader` objects exposed by `server.py` (via `rag_mcp.tools.ToolContext`) that multiple tests read and mutate.
- **Auto_Detector**: The `ProjectAutoDetector` class in `src/rag_mcp/auto_detector.py`, which scans a project directory and proposes `config.yaml` source patterns based on `config/detection_rules.json`.
- **Fresh_Checkout**: A clean clone of the Repository with no pre-existing indexed ChromaDB data, `.pytest_cache/`, or `__pycache__/` artifacts, matching the state of the CI_Workflow's `actions/checkout` step.

## Requirements

### Requirement 1: Track the Test Suite in Version Control

**User Story:** As a maintainer, I want the test suite committed to git, so that the CI_Workflow can actually collect and run tests instead of failing with "no tests ran."

#### Acceptance Criteria

1. THE Repository's `.gitignore` file SHALL NOT exclude the `tests/` directory, any file at any depth under `tests/`, or any subdirectory of `tests/` needed for test collection or execution (test modules, fixtures, and test-data files) from version control.
2. THE Repository's `.gitignore` file SHALL continue to exclude `__pycache__/` directories and `.pytest_cache/` at any depth under `tests/` from version control.
3. THE Repository SHALL contain every file needed for test collection or execution under `tests/` as a committed, tracked file in git, not merely as a file that is untracked but not excluded by `.gitignore`.
4. WHEN a Fresh_Checkout of the Repository is performed, THE Test_Suite SHALL be present on disk, with content identical to the committed version, without requiring any additional download or generation step.
5. WHEN the CI_Workflow runs `pytest` on a Fresh_Checkout, THE CI_Workflow SHALL collect at least one test.

### Requirement 2: Reorganize Tests into a Goal-Based Folder Structure

**User Story:** As a contributor, I want tests grouped into folders by the feature area they cover, so that I can find and add tests for a given part of the system without scanning a flat list of ~19 files.

#### Acceptance Criteria

1. THE Test_Folder_Structure SHALL group existing test files into the following subdirectories under `tests/`, organized by goal:
   - `tests/packaging/` — packaging, licensing, release-version, and documentation-content checks (currently `test_packaging.py`, `test_license_file.py`, `test_docs_content.py`, `test_check_release_version.py`, `test_check_release_version_cli.py`).
   - `tests/server/` — server startup, configuration, and connection checks (currently `test_mcp_connection.py`, `test_server_name.py`, `test_http_path.py`, `test_setup_process.py`).
   - `tests/mcp_tools/` — MCP tool behavior for adding, removing, and clearing indexed content (currently `test_mcp_tools.py`, `test_add_file_folder.py`, `test_add_pattern.py`, `test_remove_project.py`, `test_clear_project_index.py`).
   - `tests/indexing/` — file reading, chunking, auto-detection, and reranking logic (currently `test_chunker.py`, `test_file_reader.py`, `test_auto_detector.py`, `test_reranker.py`).
   - `tests/config/` — configuration loading (currently `test_config_loader.py`).
2. WHEN a test file is moved into a Test_Folder_Structure subdirectory, THE Test_Suite SHALL preserve every test function and test class defined in that file under unchanged names, unchanged parameters, and unchanged assertions.
3. WHEN a test file is moved into a Test_Folder_Structure subdirectory, THE Test_Folder_Structure SHALL update any path-resolution logic in that file that depends on the file's directory depth (including `sys.path` insertions and Repository-root path calculations such as `Path(__file__).parent.parent`) so that Repository-root-relative and `src`-relative paths continue to resolve correctly from the file's new location.
4. THE Test_Folder_Structure SHALL include an `__init__.py` file in `tests/` and in each new subdirectory listed in Criterion 1.
5. WHEN pytest is run from the Repository root after reorganization, THE Test_Suite SHALL report a total collected test count exactly equal to the total collected test count before reorganization, since this reorganization SHALL NOT add or remove any test function or test class.
6. IF a test file not explicitly listed in Criterion 1 contains test functions covering more than one goal area, THEN THE Test_Folder_Structure SHALL place that file in the subdirectory corresponding to the goal area exercised by the largest number of test functions in the file, SHALL break ties by placing the file in whichever tied subdirectory is listed earliest in Criterion 1, and SHALL document the placement rationale, including the goal areas considered and the test-function counts used to decide, in the file's module docstring.

### Requirement 3: Convert Repetitive Tests to a Data-Driven Style

**User Story:** As a maintainer, I want repetitive test logic expressed as parametrized data rather than duplicated functions, so that adding a new case doesn't require copy-pasting a whole test function.

#### Acceptance Criteria

1. IF two or more existing test functions within a single test module invoke the same function-under-test with the same setup/assertion sequence, the same fixtures, and the same markers, differing only in literal input values and the corresponding expected outcome, THEN THE Test_Suite SHALL express that logic as a single Data_Driven_Test using `pytest.mark.parametrize`.
2. WHEN a Data_Driven_Test replaces a set of individual test functions, THE Test_Suite SHALL preserve every distinct input/expected-outcome case that existed in the original functions.
3. WHEN a Data_Driven_Test case fails, THE Test_Suite SHALL report a pytest test ID that includes a distinguishing value from that case's parametrized input, so that the specific failing case can be identified from the test result output without inspecting the test source.
4. IF a set of test functions in a module invoke different functions-under-test, or use different setup/assertion sequences, fixtures, or markers, rather than sharing the same logic and differing only in literal input values and expected outcome, THEN THE Test_Suite SHALL leave those test functions as separate, non-parametrized tests.
5. IF a test module contains both a group of two or more functions meeting the same-logic criteria in Criterion 1 and other functions meeting the distinct-behavior criteria in Criterion 4, THEN THE Test_Suite SHALL convert the shared-logic group into a Data_Driven_Test while leaving the distinct-behavior functions unparametrized in the same module.

### Requirement 4: Fix Pre-Existing Failing Tests Caused by Hardcoded Environment Fixtures

**User Story:** As a contributor running the suite on a fresh checkout or in CI, I want tests that assume a specific developer machine's file paths and pre-indexed projects to instead set up their own isolated fixtures, so that the tests pass regardless of the machine or environment they run on.

#### Acceptance Criteria

1. IF a Failing_Test currently depends on an Environment_Fixture that does not exist in a Fresh_Checkout — including a hardcoded absolute file path, a hardcoded folder path, a pre-indexed ChromaDB project such as `my-project`, `my-project-a`, or `my-project-b`, a pre-indexed function name, or a pre-indexed hex/error code — THEN THE Test_Suite SHALL replace that Environment_Fixture with a fixture created by the test itself (e.g. a temporary directory, an in-memory/mocked indexed project, or self-indexed content), containing whatever data is needed to satisfy every assertion in that test.
2. WHEN a test in `tests/mcp_tools/` (formerly `test_add_file_folder.py` or `test_mcp_tools.py`) exercises adding, searching, comparing, or otherwise looking up indexed content — including by file path, project name, function name, or hex/error code — THE Test_Suite SHALL use values that are created or indexed within the test itself rather than assumed to pre-exist in the developer's real index.
3. IF a corrected test in `tests/mcp_tools/` still references any hardcoded absolute file path, hardcoded folder path, pre-indexed ChromaDB project name, pre-indexed function name, or pre-indexed hex/error code from the original Failing_Test, THEN THE Test_Suite SHALL treat that test as not corrected, regardless of whether it also uses fixtures created within the test itself.
4. WHEN the corrected tests from Requirement 4 are run on a Fresh_Checkout with no local indexed ChromaDB data, THE Test_Suite SHALL pass without requiring any manual indexing step beforehand, with no partially-corrected test permitted to remain failing.
5. WHEN a corrected test creates or indexes a project into the Shared_Server_Context, THE Test_Suite SHALL remove that project from the Shared_Server_Context after the test completes, regardless of whether the test passed or failed, so it does not affect the fixtures or assertions of any other test.
6. IF a Fresh_Checkout run reports a test from Requirement 4's scope as failing, THEN THE Test_Suite SHALL treat that test as not corrected, regardless of whether it passes in a developer's local environment with pre-existing indexed content.

#### Acceptance Criteria

3. THE Auto_Detector's rule-evaluation logic SHALL, for a rule defining `has_direct_files`, `patterns_if_direct`, and `patterns_if_nested` fields: check for the presence of files matching any of the `has_direct_files` glob patterns directly inside the rule's `check_dir`; return the `patterns_if_direct` patterns when at least one such file is found; and return the `patterns_if_nested` patterns when no such file is found directly inside `check_dir` but at least one file matching the same glob patterns exists in a subfolder of `check_dir`.
4. IF a rule defines `has_direct_files`, `patterns_if_direct`, and `patterns_if_nested` fields and the rule's `check_dir` does not exist, THEN THE Auto_Detector SHALL NOT match that rule and SHALL NOT return any pattern from `patterns_if_direct` or `patterns_if_nested` for it.

### Requirement 6: Fix Pre-Existing Failing Tests Caused by Shared Mutable Server State

**User Story:** As a contributor, I want project-management tests to run correctly regardless of test execution order, so that a passing suite reliably indicates correct behavior rather than depending on incidental ordering of shared global state.

#### Acceptance Criteria

1. WHEN a test in `tests/mcp_tools/` (formerly `test_clear_project_index.py` or `test_remove_project.py`) mutates the Shared_Server_Context for the duration of that test — including adding a project, replacing a mocked store/loader method, or changing an attribute on `config`, `store`, or `loader` — THE Test_Suite SHALL restore the Shared_Server_Context to its exact state from immediately before that test ran, after the test completes, regardless of whether the test passed or failed.
2. WHEN the reorganized `remove_project` tests run, THE Test_Suite SHALL create a project with a name unique to that test, invoke the `remove_project` call under test on it, and assert both that the corresponding `ProjectConfig` object's `removed` attribute equals `True` and that a subsequent `list_projects()` call (or equivalent lookup) no longer includes that project name as active, without relying on any mutation performed by another test.
3. WHEN the Test_Suite is run three times in sequence in the same process invocation, and separately run once with pytest's test order randomized (e.g. via `pytest-randomly` or an equivalent random-order plugin), THE fixed tests from this requirement SHALL pass in all four of those runs.

### Requirement 7: Fix the Flaky Server Startup Timing Assertion

**User Story:** As a contributor running the suite in CI or on a slower machine, I want the server-startup integration test to use a timing bound that tolerates normal environment variance, so that the test doesn't fail solely due to being slightly over an arbitrary threshold.

#### Acceptance Criteria

1. THE reorganized server-connection test (formerly `test_mcp_connection.py`) SHALL assert that the elapsed time between the start of launching the server subprocess (via `stdio_client`) and the completion of the MCP `session.initialize()` handshake is less than 60 seconds.
2. IF the elapsed time measured in Criterion 1 is 60 seconds or greater, THEN THE test SHALL fail and SHALL report the measured startup time, in seconds, in the failure message to aid diagnosis.
3. THE reorganized server-connection test SHALL perform the startup-time assertion described in Criteria 1-2 exactly once per test, replacing the current file's two duplicate `startup_time < 30` assertions with a single check.

### Requirement 8: Ensure the Reorganized Suite Runs Successfully in CI

**User Story:** As a maintainer, I want the reorganized and fixed test suite to pass on a fresh Linux checkout in GitHub Actions, so that the release pipeline's `test` job succeeds and subsequent build/publish jobs can run.

#### Acceptance Criteria

1. WHEN the CI_Workflow's `test` job runs `pytest` on a Fresh_Checkout — a clean clone of the Repository on the GitHub Actions Linux runner, with no pre-existing indexed ChromaDB data, `.pytest_cache/`, or `__pycache__/` artifacts — after this feature's changes are merged, THE CI_Workflow SHALL complete the `test` job with a zero exit code.
2. THE Test_Suite SHALL NOT require any locally indexed ChromaDB data, locally installed `.venv`, Windows-specific path separators, or a live network call to an external service (e.g. a real PyPI/Test PyPI lookup, or any other remote HTTP endpoint) to pass.
3. WHEN a test needs a running MCP server subprocess, THE Test_Suite SHALL determine the Python interpreter path via `sys.executable` (the interpreter currently running pytest) rather than a hardcoded OS-specific path such as `.venv/Scripts/python.exe`.
4. WHILE the CI_Workflow's `test` job runs the full reorganized Test_Suite on a Fresh_Checkout, THE CI_Workflow SHALL continue executing all remaining tests after any individual test failure, rather than stopping the run at the first failure.
5. WHEN the CI_Workflow's `test` job finishes running every test in the full reorganized Test_Suite, THE CI_Workflow SHALL report the overall pass/fail result for the run only at that point, not before all tests have finished executing.
6. WHEN the CI_Workflow's `test` job runs the full reorganized Test_Suite on a Fresh_Checkout, THE CI_Workflow SHALL report zero failing tests among the set of tests that passed when the Test_Suite was run against the codebase prior to this feature's changes, excluding tests intentionally skipped.
