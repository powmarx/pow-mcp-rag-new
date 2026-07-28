@echo off
REM RAG MCP Server - One-command setup
REM Automatically detects sibling git projects and indexes them.

echo === RAG MCP Server Setup ===
echo.

REM Detect PROJECTS_ROOT from this repo's parent directory
for %%I in ("%~dp0..") do set "PROJECTS_ROOT=%%~fI"
echo Detected PROJECTS_ROOT: %PROJECTS_ROOT%
echo.

REM Find Python (prefer Python313, fall back to PATH)
set PYTHON=C:\Python313\python.exe
if not exist "%PYTHON%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
    set PYTHON=python
)

echo [1/5] Creating virtual environment...
"%PYTHON%" -m venv "%~dp0.venv"
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo [2/5] Installing dependencies...
"%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

REM Create config.yaml from template if it doesn't exist
if not exist "%~dp0config\config.yaml" (
    echo   Creating config.yaml from template...
    copy "%~dp0config\config.template.yaml" "%~dp0config\config.yaml" >nul
)

echo [3/5] Discovering root folders (you'll choose which to index)...
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\setup_discover.py" "%PROJECTS_ROOT%"
if errorlevel 1 (
    echo WARNING: Auto-discovery had issues. Check config.yaml manually.
)

echo [4/5] Converting PDFs to Markdown...
"%~dp0.venv\Scripts\python.exe" "%~dp0indexer.py" --convert-pdfs

echo [5/5] Indexing all projects...
"%~dp0.venv\Scripts\python.exe" "%~dp0indexer.py"

echo.
REM Compute repo path early (needed for both Kiro and VS Code config)
set "REPO_PATH=%~dp0"
REM Remove trailing backslash
if "%REPO_PATH:~-1%"=="\" set "REPO_PATH=%REPO_PATH:~0,-1%"
REM Convert backslashes to forward slashes for JSON
set "REPO_FWD=%REPO_PATH:\=/%"

REM --- Kiro IDE integration (skipped if Kiro not installed) ---
if exist "%USERPROFILE%\.kiro" (
    echo [bonus] Installing MCP config for Kiro...
    if not exist "%USERPROFILE%\.kiro\settings" mkdir "%USERPROFILE%\.kiro\settings"

    REM Merge project-rag into existing mcp.json (preserves other servers)
    "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\setup_mcp_config.py" "%REPO_FWD%/.venv/Scripts/python.exe" "%REPO_FWD%/server.py"

    echo.
    echo [bonus] Installing Re-index RAG hook...
    if not exist "%~dp0.kiro\hooks" mkdir "%~dp0.kiro\hooks"

    (
    echo {
    echo   "version": "1.0.0",
    echo   "enabled": true,
    echo   "name": "Re-index RAG",
    echo   "description": "Re-indexes all configured projects in the RAG MCP server.",
    echo   "when": {
    echo     "type": "userTriggered"
    echo   },
    echo   "then": {
    echo     "type": "runCommand",
    echo     "command": "%REPO_FWD%/.venv/Scripts/python.exe %REPO_FWD%/indexer.py",
    echo     "timeout": 300
    echo   }
    echo }
    ) > "%~dp0.kiro\hooks\re-index-rag.kiro.hook"

    echo   Installed: .kiro\hooks\re-index-rag.kiro.hook
) else (
    echo [skip] Kiro not detected (~/.kiro not found), skipping MCP config and hook installation.
    echo        Install Kiro and re-run setup.bat to enable IDE integration.
)

echo.
echo [verify] Running MCP connection smoke test...
"%~dp0.venv\Scripts\python.exe" -m pytest "%~dp0tests\test_mcp_connection.py" -q --tb=short 2>nul
if errorlevel 1 (
    echo   [WARN] Some connection tests failed. Check server.py and config.yaml.
) else (
    echo   [OK] All connection tests passed!
)

echo.
echo === Setup complete! ===
echo.
echo PROJECTS_ROOT = %PROJECTS_ROOT%
echo.
echo Next steps:
if exist "%USERPROFILE%\.kiro" (
    echo   1. Restart Kiro
    echo   2. Ask Kiro questions about your projects!
) else (
    echo   1. Configure your MCP client to use:
    echo        Command: %REPO_FWD%/.venv/Scripts/python.exe
    echo        Args:    %REPO_FWD%/server.py
    echo   2. Or install Kiro and re-run setup.bat for automatic integration.
)
echo.
pause
