# Runs the scanner in MONITOR-ONLY mode (no full NSE scan).
# Polls the existing queue every 15 min during market hours and fires alerts.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Get-Content (Join-Path $root ".env") | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $k, $v = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
    }
}
$env:DB_PATH = Join-Path $root "database\cup.db"

Set-Location (Join-Path $root "scanner")
& (Join-Path $root ".venv\Scripts\python.exe") monitor_only.py
