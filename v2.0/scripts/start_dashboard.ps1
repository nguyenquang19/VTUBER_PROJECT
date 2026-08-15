$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot "venv\Scripts\python.exe"
$launcherPath = Join-Path $projectRoot "scripts\dashboard_standalone.py"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Không tìm thấy Python environment: $pythonPath"
}

Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("`"$launcherPath`"") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden
