@echo off
setlocal enabledelayedexpansion
REM RAG MCP Server - Docker setup (Phase 1)
REM Builds the image, generates a docker config (container paths), indexes all
REM sibling projects, and wires up mcp.json to launch the server via docker.
REM
REM Requires: Docker Desktop. No local Python needed.
REM
REM Usage:
REM   setup-docker.bat
REM   setup-docker.bat --src D:\SomeOtherFolder
REM   setup-docker.bat --repo D:\GitHub\pow-mcp-rag-new
REM   setup-docker.bat --image my-rag
REM   setup-docker.bat --server-name my-rag --src D:\Projects --image my-rag
REM
REM Env var fallbacks (used only if the matching flag isn't passed):
REM   set SRC=D:\SomeOtherFolder

echo === RAG MCP Server - Docker Setup ===
echo.

REM Capture the script's own folder BEFORE any argument parsing/shifting,
REM so a stray shift can never corrupt it (see: the E:\OneDrive incident).
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Parse optional arguments
set "SERVER_NAME="
set "SRC_ARG="
set "REPO_ARG="
set "IMAGE_ARG="
:parse_args
if "%~1"=="" goto end_parse
if /i "%~1"=="--server-name" (
    set "SERVER_NAME=%~2"
    shift /1
    shift /1
    goto parse_args
)
if /i "%~1"=="--src" (
    set "SRC_ARG=%~2"
    shift /1
    shift /1
    goto parse_args
)
if /i "%~1"=="--repo" (
    set "REPO_ARG=%~2"
    shift /1
    shift /1
    goto parse_args
)
if /i "%~1"=="--image" (
    set "IMAGE_ARG=%~2"
    shift /1
    shift /1
    goto parse_args
)
shift /1
goto parse_args
:end_parse

REM Resolve REPO_PATH: --repo overrides, else the script's own folder
if not "%REPO_ARG%"=="" (
    for %%I in ("%REPO_ARG%") do set "REPO_PATH=%%~fI"
) else (
    set "REPO_PATH=%SCRIPT_DIR%"
)

REM Resolve PROJECTS_ROOT: --src overrides, else SRC env var, else parent of repo
if not "%SRC_ARG%"=="" (
    for %%I in ("%SRC_ARG%") do set "PROJECTS_ROOT=%%~fI"
) else if not "%SRC%"=="" (
    for %%I in ("%SRC%") do set "PROJECTS_ROOT=%%~fI"
) else (
    for %%I in ("%REPO_PATH%\..") do set "PROJECTS_ROOT=%%~fI"
)

if not exist "%REPO_PATH%" (
    echo ERROR: Repo path does not exist: %REPO_PATH%
    pause
    exit /b 1
)
if not exist "%PROJECTS_ROOT%" (
    echo ERROR: PROJECTS_ROOT does not exist: %PROJECTS_ROOT%
    pause
    exit /b 1
)

REM Resolve IMAGE_NAME: --image overrides, else default "rag-mcp"
if not "%IMAGE_ARG%"=="" (
    set "IMAGE_NAME=%IMAGE_ARG%"
) else (
    set "IMAGE_NAME=rag-mcp"
)
set "IMAGE_TAG=%IMAGE_NAME%:latest"
set "DATA_VOLUME=%IMAGE_NAME%-data"

set "PROJECTS_FWD=%PROJECTS_ROOT:\=/%"
set "REPO_FWD=%REPO_PATH:\=/%"

echo Detected PROJECTS_ROOT: %PROJECTS_ROOT%
echo Repo path:            %REPO_PATH%
echo Image:                %IMAGE_TAG%
echo Data volume:           %DATA_VOLUME%
echo.

REM Verify Docker is available
docker version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker not found or not running. Start Docker Desktop and retry.
    pause
    exit /b 1
)

echo [1/4] Building image %IMAGE_TAG% ...
docker build -t %IMAGE_TAG% "%REPO_PATH%"
if errorlevel 1 (
    echo ERROR: Docker build failed.
    pause
    exit /b 1
)

echo.
echo [2/4] Discovering projects (container paths under /projects) ...
docker run --rm ^
    -v "%PROJECTS_FWD%:/projects:ro" ^
    -v "%DATA_VOLUME%:/app/data" ^
    %IMAGE_TAG% ^
    python scripts/setup_discover.py /projects --config /app/data/config.yaml --list

echo.
set "SELECTION=all"
set /p "SELECTION=Select folders to index (comma-separated numbers, 'all', or 'none') [all]: "

docker run --rm ^
    -v "%PROJECTS_FWD%:/projects:ro" ^
    -v "%DATA_VOLUME%:/app/data" ^
    %IMAGE_TAG% ^
    python scripts/setup_discover.py /projects --config /app/data/config.yaml --select "%SELECTION%"
if errorlevel 1 (
    echo WARNING: Discovery had issues.
)

echo.
echo [3/4] Indexing selected projects into volume %DATA_VOLUME% ...
docker run --rm ^
    -v "%PROJECTS_FWD%:/projects:ro" ^
    -v "%DATA_VOLUME%:/app/data" ^
    %IMAGE_TAG% ^
    python indexer.py
if errorlevel 1 (
    echo WARNING: Indexing had issues. Review the output above.
)

echo.
echo [4/4] Updating mcp.json for Kiro (docker command) ...
if exist "%USERPROFILE%\.kiro" (
    if not exist "%USERPROFILE%\.kiro\settings" mkdir "%USERPROFILE%\.kiro\settings"
    set "SERVER_NAME_ARG="
    if not "!SERVER_NAME!"=="" set "SERVER_NAME_ARG=--server-name !SERVER_NAME!"
    docker run --rm ^
        -v "%USERPROFILE%\.kiro:/hostkiro" ^
        %IMAGE_TAG% ^
        python scripts/setup_mcp_config.py --docker ^
            --projects-dir "%PROJECTS_FWD%" ^
            --image %IMAGE_TAG% ^
            --data-volume %DATA_VOLUME% ^
            --out /hostkiro/settings/mcp.json ^
            !SERVER_NAME_ARG!
) else (
    echo [skip] Kiro not detected ^(~/.kiro not found^), skipping mcp.json update.
)

echo.
echo === Docker setup complete! ===
echo.
echo Image:         %IMAGE_TAG%
echo MCP name:      %SERVER_NAME%
if "%SERVER_NAME%"=="" echo MCP name:      rag-mcp (default)
echo Projects root: %PROJECTS_ROOT%
echo Config:        %REPO_PATH%\config\config.yaml
echo Data volume:   %DATA_VOLUME%
echo.
echo Next steps:
echo   1. Restart Kiro
echo   2. Ask Kiro questions about your projects!
echo.
echo To re-index later:
echo   set PROJECTS_DIR=%PROJECTS_FWD%
echo   docker compose run --rm indexer
echo.
pause