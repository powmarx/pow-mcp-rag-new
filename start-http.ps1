# start-http.ps1
# Starts the rag-mcp HTTP server, probing for a free host port starting at 8000.
# Mirrors the server internal port-probing logic so Docker -p mapping is always valid.
# Optionally updates mcp.json with the chosen URL so Kiro picks it up.
#
# Usage:
#   .\start-http.ps1                              # default port 8000, name rag-mcp-http
#   .\start-http.ps1 -StartPort 8080              # try from 8080 upward
#   .\start-http.ps1 -Name my-rag                 # custom container name
#   .\start-http.ps1 -ServerName my-rag-mcp       # custom MCP server key in mcp.json
#   .\start-http.ps1 -UpdateMcp                   # write URL into mcp.json after start

param(
    [int]$StartPort    = 8000,
    [int]$MaxTries     = 10,
    [string]$Name      = "rag-mcp-http",
    [string]$SRC       = "C:/GIT",
    [string]$ServerName = "",          # MCP key in mcp.json; empty = read from server_info.json
    [string]$HttpPath  = "/mcp",       # HTTP endpoint path (MCP_HTTP_PATH env var in container)
    [switch]$UpdateMcp                 # if set, update ~/.kiro/settings/mcp.json with the HTTP URL
)

# --- find a free host port ---
$port = $null
for ($p = $StartPort; $p -lt ($StartPort + $MaxTries); $p++) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $p)
        $listener.Start()
        $port = $p
        $listener.Stop()
        break
    } catch {
        # port in use, try next
    } finally {
        if ($listener -and $listener.Server.IsBound) { $listener.Stop() }
    }
}

if (-not $port) {
    Write-Error "No free port found in range $StartPort-$($StartPort + $MaxTries - 1). Aborting."
    exit 1
}

if ($port -ne $StartPort) {
    Write-Host "[probe] Port $StartPort in use -- using $port instead"
}

# --- remove old container with same name if stopped ---
$existing = docker ps -a --filter "name=^$Name`$" --format "{{.Status}}" 2>$null
if ($existing -and $existing -notmatch "^Up") {
    Write-Host "[docker] Removing stopped container '$Name'"
    docker rm $Name | Out-Null
}

# --- build port mapping string separately to avoid PS parsing issues ---
$portMap = "${port}:8000"
$srcMap  = "${SRC}:/projects:ro"

Write-Host "[docker] Starting '$Name' on http://localhost:$port$HttpPath"

docker run -d --name $Name `
    -p $portMap `
    -v $srcMap `
    -v "rag-mcp-new-pip-data:/app/data" `
    -e "MCP_HTTP_PORT=8000" `
    -e "MCP_HTTP_PATH=$HttpPath" `
    --restart unless-stopped `
    rag-mcp-new-pip:latest python server.py --http --no-reindex

if ($LASTEXITCODE -ne 0) {
    Write-Error "[error] docker run failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host "[done] Server running at http://localhost:$port$HttpPath"

# --- optionally update mcp.json ---
if ($UpdateMcp) {
    $repoDir = $PSScriptRoot
    $mcpPath = "$env:USERPROFILE\.kiro\settings\mcp.json"
    $url     = "http://localhost:$port$HttpPath"

    # Resolve the server name: parameter > server_info.json
    $effectiveServerName = $ServerName
    if (-not $effectiveServerName) {
        $infoPath = Join-Path $repoDir "config\server_info.json"
        if (Test-Path $infoPath) {
            $effectiveServerName = (Get-Content $infoPath | ConvertFrom-Json).name
        } else {
            $effectiveServerName = "rag-mcp"
        }
    }

    # Read existing mcp.json or start fresh
    if (Test-Path $mcpPath) {
        $config = Get-Content $mcpPath -Raw | ConvertFrom-Json
    } else {
        $config = [PSCustomObject]@{ mcpServers = [PSCustomObject]@{} }
    }
    if (-not $config.mcpServers) {
        $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([PSCustomObject]@{})
    }

    # Build the HTTP MCP entry
    $entry = [PSCustomObject]@{
        url          = $url
        disabled     = $false
        autoApprove  = @("search_docs","list_projects","list_files","get_document",
                         "search_specs","search_code","find_function","find_variable",
                         "search_hex_pattern","compare_projects","get_project_summary",
                         "add_project","add_file","add_folder","add_pattern",
                         "index_log_file","search_logs")
    }

    # Add or update the server entry
    if ($config.mcpServers.PSObject.Properties[$effectiveServerName]) {
        $config.mcpServers.PSObject.Properties[$effectiveServerName].Value = $entry
        Write-Host "[mcp.json] Updated '$effectiveServerName' -> $url"
    } else {
        $config.mcpServers | Add-Member -NotePropertyName $effectiveServerName -NotePropertyValue $entry
        Write-Host "[mcp.json] Added '$effectiveServerName' -> $url"
    }

    # Write back
    $null = New-Item -ItemType Directory -Force -Path (Split-Path $mcpPath)
    $config | ConvertTo-Json -Depth 10 | Set-Content $mcpPath -Encoding UTF8
    Write-Host "[mcp.json] Saved to $mcpPath"
    Write-Host "[mcp.json] Restart Kiro to apply."
}
