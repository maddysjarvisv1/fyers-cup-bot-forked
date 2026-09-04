# Runs the FastAPI backend natively (no Docker) on http://localhost:8000
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

# Make sure the schema exists before serving
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "scanner\db.py")

Set-Location (Join-Path $root "dashboard\backend")
& (Join-Path $root ".venv\Scripts\python.exe") -m uvicorn main:app --host 0.0.0.0 --port 8000
