# One-time full-NSE cup-setup scan (rebuilds the queue with genuine 3-yr-ATH setups).
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
$env:PYTHONUNBUFFERED = "1"
Set-Location (Join-Path $root "scanner")
& (Join-Path $root ".venv\Scripts\python.exe") full_scan_once.py
