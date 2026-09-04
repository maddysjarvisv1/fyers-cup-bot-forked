# Runs the React dashboard dev server (Vite) on http://localhost:5173
# Vite proxies /api -> http://localhost:8000 (the backend), per vite.config.js
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location (Join-Path $root "dashboard\frontend")
if (-not (Test-Path "node_modules")) {
    npm install
}
npm run dev
