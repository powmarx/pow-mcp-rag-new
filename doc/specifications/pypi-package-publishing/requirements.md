# Requirements Document

## Introduction

The project currently ships as `rag-mcp` on PyPI's namespace only in name — it has never been
published. Users can only run it from a local checkout (Docker, a local `pypiserver` index, or a
manually built wheel). The desired PyPI distribution name `rag-mcp` is already registered by an
unrelated package, and the alternative `rag-mcp-server` is also already registered by a
different, unrelated package, so this feature publishes the project to public PyPI under the
available name `pow-rag-mcp`, while keeping the importable package (`rag_mcp`) and the console
script (`rag-mcp`) unchanged so existing usage patterns and documentation keep working. This
feature covers packaging metadata correctness (including replacing the current `"Proprietary"`
license with an open-source Apache License 2.0), a documented versioning strategy, an automated GitHub
Actions release workflow using PyPI Trusted Publishing, pre-publish verification through Test
PyPI, and post-publish verification that `pip`, `uv tool install`, and `uvx` all produce a working
install.

## Glossary

- **Project**: The `pow-mcp-rag-new` repository and the software it produces.
- **Pyproject_Toml**: The `pyproject.toml` file at the repository root that declares build
  configuration and package metadata.
- **Package_Build**: The sdist and wheel artifacts produced from the Pyproject_Toml
  configuration.
- **Build_Validator**: The tooling step that inspects built artifacts (e.g. metadata and
  description rendering checks) and reports errors prior to upload.
- **Distribution_Name**: The name under which the package is published on PyPI
  (`pow-rag-mcp`), as declared in `project.name`.
- **Import_Package**: The importable Python package (`rag_mcp`) located under `src/`.
- **Console_Script**: The command-line executable (`rag-mcp`) installed via the
  `project.scripts` entry point.
- **CLI_Entry_Point**: The `rag_mcp.cli:main` function invoked by the Console_Script.
- **Package_Manager**: `pip`, `uv`, or `uvx`, used by an end user to install or run the
  package.
- **Release_Workflow**: The GitHub Actions workflow that builds, validates, and publishes
  Package_Build artifacts.
- **Version_Tag**: A Git tag matching the pattern `v<version>` that triggers the
  Release_Workflow.
- **Maintainer**: A person with permission to cut releases of the Project.
- **Test_PyPI**: The `test.pypi.org` package index used for pre-publish verification.
- **PyPI**: The production `pypi.org` package index.
- **Trusted_Publisher**: A PyPI or Test_PyPI configuration entry that authorizes a specific
  GitHub repository and workflow to publish via OpenID Connect, without a stored API token.
- **Readme**: The `README.md` file at the repository root.
- **Pip_Install_Guide**: The `doc/PIP_INSTALL_GUIDE.md` file.
- **Verification_Procedure**: The documented steps used to confirm a published release installs
  and runs correctly.

## Requirements

### Requirement 1: Public-Ready Package Metadata

**User Story:** As a maintainer, I want the package metadata to meet PyPI's public distribution
requirements, so that the package can be published and discovered correctly on PyPI.

#### Acceptance Criteria

1. THE Pyproject_Toml SHALL declare `project.name` as `"pow-rag-mcp"`.
2. THE Pyproject_Toml SHALL declare the Readme content as the PyPI long description and SHALL
   declare a matching long description content-type of `text/markdown`.
3. THE Pyproject_Toml SHALL declare Trove classifiers that identify the supported Python
   versions and the development status, specifically: one Python version classifier for each
   minor version permitted by `project.requires-python` and exactly one Development Status
   classifier. THE Pyproject_Toml SHALL NOT declare a `"License :: OSI Approved :: ..."`
   classifier, since combining one with the SPDX `project.license` string required by
   Requirement 2 raises a build error under setuptools' PEP 639 enforcement (setuptools
   `>=77.0.3`, within this project's pinned `setuptools>=68,<82` build requirement) — the SPDX
   string is the sole source of truth for the license identification required by Requirement 2.
4. WHEN the Package_Build produces a wheel and a source distribution, THE Build_Validator SHALL
   confirm both artifacts pass metadata validation and that the long description renders
   successfully, before upload.
5. THE Release_Workflow SHALL run the Build_Validator as a step separate from and after the
   build step, so the build step's own artifact production is independent of validation
   outcome.
6. IF the Build_Validator reports a metadata error, THEN THE Release_Workflow SHALL stop before
   any upload step.

### Requirement 2: Open Source License

**User Story:** As a maintainer, I want the project to carry a clear open source license, so
that public PyPI users know their rights to use, modify, and distribute the package.

#### Acceptance Criteria

1. THE Repository SHALL contain a `LICENSE` file at the repository root containing the full
   text of the Apache License 2.0, with the copyright year and copyright holder filled in, and
   SHALL NOT contain unresolved placeholder text such as `[yyyy]` or `[name of copyright owner]`.
2. THE Pyproject_Toml SHALL declare the SPDX license identifier `"Apache-2.0"` in
   `project.license`, replacing the current `"Proprietary"` value.
3. THE Pyproject_Toml SHALL NOT include a `"License :: OSI Approved :: ..."` classifier
   alongside the SPDX `project.license` string, per Requirement 1 criterion 3 — the SPDX
   string alone identifies the license to PyPI and to tooling.
4. THE Readme SHALL name the license as "Apache License 2.0" and SHALL include a hyperlink to
   the repository-root `LICENSE` file.
5. WHEN the Package_Build is produced after the license metadata change in the Pyproject_Toml,
   THE Build_Validator SHALL confirm the resulting wheel and source distribution pass metadata
   validation.

### Requirement 3: Distribution Name Separate from Import Package and Console Script

**User Story:** As a user installing via pip or uv, I want to install the package under an
available PyPI name while still running the familiar command, so that installation doesn't
collide with the unrelated package already published as `rag-mcp`.

#### Acceptance Criteria

1. THE Pyproject_Toml SHALL declare `project.name` as `"pow-rag-mcp"`.
2. THE Pyproject_Toml SHALL declare the Import_Package as `rag_mcp`, distinct from the
   Distribution_Name `pow-rag-mcp` declared in criterion 1.
3. THE Pyproject_Toml SHALL declare the Console_Script entry point `"rag-mcp"` pointing to
   `rag_mcp.cli:main`.
4. WHEN a user runs `uvx --from pow-rag-mcp rag-mcp serve`, THE CLI_Entry_Point SHALL start
   the MCP server process and accept MCP protocol requests over the stdio transport.
5. IF the `uvx --from pow-rag-mcp rag-mcp serve` invocation cannot resolve the
   Distribution_Name `pow-rag-mcp` on the configured package index, THEN THE Package_Manager
   SHALL exit with a non-zero status and SHALL NOT start the MCP server.
6. WHEN a user runs `uv tool install pow-rag-mcp`, THE Package_Manager SHALL install an
   executable named `rag-mcp` on the user's PATH pointing to the CLI_Entry_Point.
7. IF the `uv tool install pow-rag-mcp` command cannot resolve the Distribution_Name
   `pow-rag-mcp` on the configured package index, THEN THE Package_Manager SHALL exit with a
   non-zero status and SHALL NOT create the `rag-mcp` executable.
8. THE Readme SHALL state, in its installation instructions, that the PyPI Distribution_Name is
   `pow-rag-mcp` and that the command to run after install is `rag-mcp`, to distinguish it
   from the unrelated `rag-mcp` and `rag-mcp-server` packages already on PyPI.

### Requirement 4: Versioning Strategy

**User Story:** As a maintainer, I want a documented, single-source versioning strategy, so
that every published release has an unambiguous, correctly ordered version number.

#### Acceptance Criteria

1. THE Project SHALL follow Semantic Versioning (MAJOR.MINOR.PATCH, with optional pre-release
   and build metadata suffixes) for every published release.
2. THE Pyproject_Toml `project.version` field SHALL be the single source of truth for the
   package version used by the Package_Build.
3. THE Release_Workflow SHALL derive the version to publish from a Version_Tag matching the
   pattern `v<version>`, by stripping the leading `v` from the Version_Tag, where `<version>`
   SHALL be a valid Semantic Versioning string as defined in criterion 1.
4. IF the version derived from the Version_Tag does not exactly match the Pyproject_Toml
   `project.version` string, THEN THE Release_Workflow SHALL fail with an error before building
   or publishing artifacts.
5. IF the Pyproject_Toml `project.version` value is not a valid Semantic Versioning string as
   defined in criterion 1, THEN THE Release_Workflow SHALL fail with an error before building or
   publishing artifacts.
6. IF the version derived from the Version_Tag is not greater than the version of the most
   recently published PyPI release, per Semantic Versioning precedence rules, THEN THE
   Release_Workflow SHALL fail with an error before publishing artifacts.

### Requirement 5: Automated Release Workflow

**User Story:** As a maintainer, I want an automated GitHub Actions workflow to build and
publish the package, so that releases are consistent and don't require manual upload steps.

#### Acceptance Criteria

1. WHEN a Maintainer pushes a Version_Tag to the Repository, THE Release_Workflow SHALL run
   automatically.
2. THE Release_Workflow SHALL run the Project's automated test suite before building release
   artifacts.
3. IF the test suite fails, a build step fails, or a validation step reports an error, THEN THE
   Release_Workflow SHALL mark the workflow run as failed and confirm that no artifacts are
   published to Test_PyPI or PyPI.
4. THE Release_Workflow SHALL build both a wheel and a source distribution.
5. WHEN the build, validation, and Test_PyPI verification (Requirement 7) all succeed, THE
   Release_Workflow SHALL publish the artifacts to PyPI.
6. IF the upload step to Test_PyPI or to PyPI itself fails after all preceding validation
   steps succeeded, THEN THE Release_Workflow SHALL stop, mark the workflow run as failed, and
   SHALL NOT proceed to the next publish step.
7. THE Release_Workflow SHALL support being triggered manually by a Maintainer, and SHALL make
   the failure state and reason of any run visible in the GitHub Actions run history.
8. IF the Release_Workflow fails to start after a Version_Tag push (e.g. due to GitHub Actions
   unavailability, repository permission errors, or workflow configuration errors), THEN THE
   Maintainer SHALL resolve the underlying issue and either re-push the Version_Tag or manually
   trigger the Release_Workflow using the capability in criterion 7.

### Requirement 6: Trusted Publishing Credentials

**User Story:** As a maintainer, I want to publish to PyPI without storing long-lived API
tokens, so that the release process stays secure.

#### Acceptance Criteria

1. THE Release_Workflow SHALL authenticate to PyPI and to Test_PyPI using OpenID Connect
   Trusted Publishing.
2. THE Release_Workflow SHALL NOT use a stored PyPI or Test_PyPI API token for authentication.
3. THE Maintainer SHALL configure a PyPI Trusted_Publisher entry scoped to this Repository, the
   Release_Workflow's workflow filename, and a named GitHub Actions publishing environment that
   the PyPI publish job runs under.
4. THE Maintainer SHALL configure a separate Test_PyPI Trusted_Publisher entry scoped to this
   Repository and the Release_Workflow's workflow filename; a named GitHub Actions publishing
   environment SHALL NOT be required for the Test_PyPI Trusted_Publisher entry.
5. IF PyPI or Test_PyPI rejects the OpenID Connect authentication at publish time (e.g. due to a
   Trusted_Publisher configuration mismatch or an unrecognized OIDC claim), THEN THE
   Release_Workflow SHALL stop without publishing artifacts and SHALL surface an error
   indicating the authentication failure.

### Requirement 7: Pre-Publish Verification via Test PyPI

**User Story:** As a maintainer, I want a release verified on Test PyPI before it reaches
production PyPI, so that packaging problems are caught before reaching real users.

#### Acceptance Criteria

1. THE Release_Workflow SHALL publish Package_Build artifacts to Test_PyPI before publishing
   the same artifacts to PyPI.
2. WHEN artifacts are published to Test_PyPI, THE Release_Workflow SHALL install the package
   from Test_PyPI into a clean environment that has no pre-existing installation of the
   Import_Package or the Console_Script.
3. WHEN installing the package from Test_PyPI, THE Release_Workflow SHALL resolve the
   package's dependencies from PyPI rather than from Test_PyPI, since Test_PyPI frequently does
   not host the same dependency versions.
4. AFTER installing the package from Test_PyPI, THE Release_Workflow SHALL invoke the
   Console_Script.
5. IF the Test_PyPI install exits with a non-success status or produces any error output, THEN
   THE Release_Workflow SHALL stop without publishing to PyPI.
6. IF the Console_Script invocation in criterion 4 exits with a non-success status or produces
   any error output, THEN THE Release_Workflow SHALL stop without publishing to PyPI.

### Requirement 8: Fresh Environment Install Verification

**User Story:** As a maintainer, I want to confirm the published package installs and runs
correctly with pip and uv, so that end users have a working experience via uvx.

#### Acceptance Criteria

1. WHEN the package is installed from PyPI with `pip install pow-rag-mcp` into a clean
   virtual environment, THE `pip install` command SHALL exit with a success status and THE
   Console_Script SHALL be resolvable on the environment's PATH.
2. WHEN the pip-installed Console_Script is invoked, THE Console_Script SHALL exit with a
   success status and SHALL NOT produce a traceback.
3. WHEN a user runs `uvx --from pow-rag-mcp rag-mcp config`, THE CLI_Entry_Point SHALL exit
   with a success status, SHALL print the resolved config and data paths, and SHALL NOT produce
   a traceback.
4. WHEN a user runs `uv tool install pow-rag-mcp`, THE Package_Manager SHALL exit with a
   success status, SHALL install a `rag-mcp` executable, and invoking that executable SHALL
   exit with a success status and SHALL NOT produce a traceback.
5. IF any check in criteria 1 through 4 fails, THEN THE Maintainer SHALL treat the release as
   failed and remediate the issue, since the package is already published on PyPI at this
   point.
6. THE Project SHALL document the Verification_Procedure, referencing the concrete commands and
   expected exit codes and outputs from criteria 1 through 4, so a Maintainer can repeat it
   manually for any release.

### Requirement 9: Documentation Updates for Public PyPI Installation

**User Story:** As a new user, I want clear, accurate installation instructions for public
PyPI, so that I can install the tool with uvx or pip without needing a local index or a repo
checkout.

#### Acceptance Criteria

1. THE Readme SHALL document `uvx --from pow-rag-mcp rag-mcp serve` and
   `uv tool install pow-rag-mcp` as installation methods that require no repo checkout and no
   local package index configuration.
2. THE Readme SHALL document `pip install pow-rag-mcp` as an alternative installation method
   that requires no repo checkout and no local package index configuration.
3. THE Pip_Install_Guide SHALL contain two distinct sections: a public PyPI installation flow
   section and a local-index installation flow section, each documenting that flow's specific
   commands and prerequisites.
4. THE Pip_Install_Guide SHALL recommend the public PyPI flow for new users who have no repo
   checkout, and SHALL recommend the local-index flow for maintainers or contributors testing
   unpublished changes.
5. THE Readme SHALL state the minimum supported Python version required to install the
   package, and this stated version SHALL match the `requires-python` value declared in the
   Pyproject_Toml.

### Requirement 10: Local Release Dry Run

**User Story:** As a maintainer, I want to build and validate the package locally before
pushing a release tag, so that I can catch packaging mistakes without consuming a release
attempt.

#### Acceptance Criteria

1. THE Project SHALL provide a documented local command sequence that builds the sdist and
   wheel and does not include any upload or publish command.
2. WHEN a Maintainer runs the local build, THE Build_Validator SHALL confirm both artifacts
   pass metadata validation before any upload step is reachable.
3. IF the Build_Validator reports a metadata error during the local build, THEN THE
   Build_Validator SHALL report the specific error to the Maintainer before any upload step is
   reachable.
