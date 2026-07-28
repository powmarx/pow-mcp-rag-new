# diagnose-chromadb.ps1
# Runs scripts/diagnose_chromadb.py inside the rag-mcp-new-pip:latest image against
# the live rag-mcp-new-pip-data volume. Safe to run while the MCP server / Kiro is
# using the same volume (read-only checks; the per-collection probe runs in
# throwaway subprocesses so a crash there doesn't affect your running server).
#
# Usage:
#   .\scripts\diagnose-chromadb.ps1                                   # full scan, all projects
#   .\scripts\diagnose-chromadb.ps1 -Project project_A
#   .\scripts\diagnose-chromadb.ps1 -SkipProbe                        # fast, skips count/query checks
#   .\scripts\diagnose-chromadb.ps1 -Volume rag-mcp-new-pip-data  # check a different volume

param(
    [string]$Project = "",
    [switch]$SkipProbe,
    [string]$Volume = "rag-mcp-new-pip-data",
    [string]$Image = "rag-mcp-new-pip:latest"
)

$scriptDir = $PSScriptRoot
$scriptPath = Join-Path $scriptDir "diagnose_chromadb.py"

if (-not (Test-Path $scriptPath)) {
    Write-Error "diagnose_chromadb.py not found next to this script at $scriptPath"
    exit 1
}

$dockerArgs = @(
    "run", "--rm",
    "-v", "${Volume}:/app/data",
    "-v", "${scriptPath}:/tmp/diagnose_chromadb.py:ro",
    $Image,
    "python", "/tmp/diagnose_chromadb.py"
)

if ($Project) {
    $dockerArgs += @("--project", $Project)
}
if ($SkipProbe) {
    $dockerArgs += "--skip-probe"
}

Write-Host "[diagnose] Running against volume '$Volume' using image '$Image'..."
& docker @dockerArgs
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "`n[diagnose] Done - no issues found." -ForegroundColor Green
} else {
    Write-Host "`n[diagnose] Done - issues were found, see output above." -ForegroundColor Yellow
}

exit $exitCode
