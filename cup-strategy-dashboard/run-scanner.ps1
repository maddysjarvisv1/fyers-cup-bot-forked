# Runs the Scanner microservice natively (no Docker).
# WARNING: on startup this scans the full NSE universe (~2000 stocks, 10+ min).
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Load variables from .env
Get-Content (Join-Path $root ".env") | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $k, $v = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
    }
}

# Override the Docker-container DB path with a local one
$env:DB_PATH = Join-Path $root "database\cup.db"

Set-Location (Join-Path $root "scanner")
& (Join-Path $root ".venv\Scripts\python.exe") main.py
