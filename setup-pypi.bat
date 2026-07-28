@echo off
setlocal enabledelayedexpansion
REM RAG MCP Server - Local PyPI setup (pip/uvx mode)
REM Builds the wheel, publishes it to a local pypiserver index, and wires up
REM mcp.json / .vscode/mcp.json to launch the server via `uvx` — no Docker,
REM no local venv needed on the machine that connects to Kiro (uvx manages
REM its own isolated environment and caches it).
REM
REM This is a stepping stone: the local index (http://localhost:8080) is
REM meant to be swapped later for a hosted index (S3 + static index, or
REM AWS CodeArtifact) by changing only --index-url below and in mcp.json.
REM
REM Requires: uv / uvx (https://docs.astral.sh/uv/getting-started/installation/)
REM
REM Usage:
REM   setup-pypi.bat                          (uses default server name: rag-mcp)
REM   setup-pypi.bat --server-name my-rag     (custom MCP server key in mcp.json)
REM   setup-pypi.bat --port 8090              (custom pypiserver port)
REM
REM mcp.json is pointed at a persistent 'uv tool install'-ed exe by default
REM (avoids the intermittent Windows Defender trampoline race that 'uvx --from'
REM hits on every launch with this package's large dependency tree — see
REM doc/TROUBLESHOOTING.md). Falls back to 'uvx --from' automatically if
REM 'uv tool install' isn't available or fails.

echo === RAG MCP Server - Local PyPI Setup ===
echo.

REM Parse optional arguments
set "SERVER_NAME="
set "PORT=8080"
:parse_args
if "%~1"=="" goto end_parse
if /i "%~1"=="--server-name" (
    set "SERVER_NAME=%~2"
    shift
    shift
    goto parse_args
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
shift
goto parse_args
:end_parse

set "REPO_PATH=%~dp0"
if "%REPO_PATH:~-1%"=="\" set "REPO_PATH=%REPO_PATH:~0,-1%"
set "REPO_FWD=%REPO_PATH:\=/%"
set "INDEX_URL=http://localhost:%PORT%/simple/"

REM Find Python (prefer Python313, fall back to PATH) — only needed to build
REM the wheel and run pypiserver; the *installed* server runs via uvx and
REM needs no persistent venv.
set PYTHON=C:\Python313\python.exe
if not exist "%PYTHON%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found. Install Python 3.11+ and try again.
        pause
        exit /b 1
    )
    set PYTHON=python
)

where uvx >nul 2>&1
if errorlevel 1 (
    echo ERROR: uvx not found. Install uv: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [1/7] Creating build virtual environment...
    "%PYTHON%" -m venv "%~dp0.venv"
) else (
    echo [1/7] Build virtual environment already exists.
)

echo [2/7] Installing build tools ^(build, pypiserver^)...
"%~dp0.venv\Scripts\pip.exe" install --quiet build pypiserver

echo [3/7] Syncing config templates into the package ^(src/rag_mcp/data/^)...
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\sync_package_data.py"

echo [4/7] Building wheel...
if exist "%~dp0dist" del /q "%~dp0dist\*.whl" >nul 2>&1
"%~dp0.venv\Scripts\python.exe" -m build --wheel "%REPO_PATH%" --outdir "%REPO_PATH%\dist"
if errorlevel 1 (
    echo ERROR: Wheel build failed.
    pause
    exit /b 1
)

echo [5/7] Publishing wheel to local index ^(packages\^)...
if not exist "%~dp0packages" mkdir "%~dp0packages"
for %%F in ("%~dp0dist\*.whl") do copy /y "%%F" "%~dp0packages\" >nul

echo.
echo   Local PyPI index will serve from: %~dp0packages
echo   Start it manually if not already running:
echo     .venv\Scripts\python.exe -m pypiserver run -p %PORT% packages --disable-fallback
echo.
echo   (Next phase: swap %INDEX_URL% for an S3-hosted or CodeArtifact index —
echo    only --index-url changes, uvx and mcp.json setup stay the same.)
echo.

echo [6/7] Installing as a persistent uv tool ^(avoids per-launch re-resolution^)...
REM `uvx --from` re-resolves the ~110-package dependency tree on every MCP
REM connection, which on Windows intermittently races Defender's scanner on
REM the trampoline .exe write ("Failed to update Windows PE resources...
REM Acesso negado" — see doc/TROUBLESHOOTING.md). `uv tool install` resolves
REM ONCE into a persistent env and drops a stable exe at ~/.local/bin — same
REM pattern as a plain global pip install (e.g. this repo's graphify entries).
REM The race is transient and package-agnostic (varies: pywin32, cffi,
REM uvicorn, torch...) so retry automatically a few times before giving up.
where uv >nul 2>&1
if errorlevel 1 (
    echo   [warn] 'uv' not found on PATH ^(only uvx was checked earlier^) — skipping stable tool install.
    echo          mcp.json will use 'uvx --from', which re-resolves on every launch.
    set "STABLE_ARG="
) else (
    set "STABLE_ARG="
    for /l %%A in (1,1,5) do (
        if "!STABLE_ARG!"=="" (
            uv tool install --extra-index-url "%INDEX_URL%" --force rag-mcp
            if not errorlevel 1 (
                set "STABLE_ARG=--stable"
            ) else (
                echo   [retry %%A/5] Install failed ^(likely a transient Windows Defender lock on
                echo               uv's trampoline exe — see doc/TROUBLESHOOTING.md^). Retrying...
                timeout /t 3 /nobreak >nul
            )
        )
    )
    if "!STABLE_ARG!"=="" (
        echo   [warn] 'uv tool install' failed after 5 attempts. Falling back to 'uvx --from' in
        echo          mcp.json ^(re-resolves on every launch instead of once^). Re-run this script
        echo          later to retry the stable install, or try manually:
        echo            uv tool install --extra-index-url "%INDEX_URL%" --force rag-mcp
    )
)

echo.
echo [7/7] Updating mcp.json...
if exist "%USERPROFILE%\.kiro" (
    if not exist "%USERPROFILE%\.kiro\settings" mkdir "%USERPROFILE%\.kiro\settings"
    set "SERVER_NAME_ARG="
    if not "%SERVER_NAME%"=="" set "SERVER_NAME_ARG=--server-name %SERVER_NAME%"
    "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\setup_mcp_config.py" --uvx ^
        --index-url "%INDEX_URL%" ^
        --package rag-mcp ^
        --vscode !STABLE_ARG! ^
        !SERVER_NAME_ARG!
) else (
    echo [skip] Kiro not detected ^(~/.kiro not found^), skipping mcp.json update.
    echo        Still writing .vscode/mcp.json for VS Code / VS 2026 ^(uses this repo's index^).
    "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\setup_mcp_config.py" --uvx ^
        --index-url "%INDEX_URL%" ^
        --package rag-mcp ^
        --vscode !STABLE_ARG! ^
        --out "%TEMP%\rag-mcp-skip.json"
    del "%TEMP%\rag-mcp-skip.json" >nul 2>&1
)

echo.
echo === Local PyPI setup complete! ===
echo.
echo Index URL:   %INDEX_URL%
echo Package dir: %REPO_FWD%/packages
echo.
echo IMPORTANT: keep the local index server running while Kiro/VS Code connect:
echo   .venv\Scripts\python.exe -m pypiserver run -p %PORT% packages --disable-fallback
echo.
echo Next steps:
echo   1. Start the pypiserver (see command above) — or run it as a background task
echo   2. Restart Kiro (or reconnect the MCP server)
if not "!STABLE_ARG!"=="" (
    echo   3. mcp.json points at the persistent tool exe — starts instantly, no re-resolution
) else (
    echo   3. mcp.json uses 'uvx --from' — first connection downloads deps (~10-30s), cached after
)
echo.
echo To rebuild and republish after code changes, re-run this script.
echo.
pause
