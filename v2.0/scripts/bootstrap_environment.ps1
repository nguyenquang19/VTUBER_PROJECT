[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet("Full", "CI")]
    [string]$Profile = "Full",

    [Parameter()]
    [ValidatePattern("^3\.11\.\d+$")]
    [string]$PythonVersion = "3.11.15",

    [Parameter()]
    [switch]$SkipPythonInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$currentVenv = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "venv"))
$stagingVenv = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "venv.next"))
$cacheDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".uv-cache"))

foreach ($target in @($currentVenv, $stagingVenv, $cacheDir)) {
    $parent = [System.IO.Path]::GetFullPath((Split-Path -Parent $target))
    if ($parent -ne $repoRoot) {
        throw "Unsafe environment path outside repository root: $target"
    }
}

if (Test-Path -LiteralPath $stagingVenv) {
    throw "Staging environment already exists: $stagingVenv. Inspect it before retrying."
}

$uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw "uv.exe was not found on PATH. Install uv before bootstrapping Mai."
}
$uv = $uvCommand.Source

$requirements = if ($Profile -eq "Full") {
    Join-Path $repoRoot "requirements.lock.txt"
} else {
    Join-Path $repoRoot "requirements-ci.txt"
}
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    throw "Requirements file not found: $requirements"
}

$env:UV_CACHE_DIR = $cacheDir
New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null

if (-not $SkipPythonInstall) {
    & $uv python install $PythonVersion
    if ($LASTEXITCODE -ne 0) {
        throw "uv could not install Python $PythonVersion."
    }
}

$basePythonOutput = & $uv python find $PythonVersion --managed-python 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "uv could not find managed Python $($PythonVersion): $(($basePythonOutput | Out-String).Trim())"
}
$basePython = (($basePythonOutput | Select-Object -Last 1) -as [string]).Trim()
if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
    throw "uv returned an invalid Python path: $basePython"
}

& $uv venv $stagingVenv --python $basePython --seed
if ($LASTEXITCODE -ne 0) {
    throw "Could not create staging environment: $stagingVenv"
}

$stagingPython = Join-Path $stagingVenv "Scripts\python.exe"
& $uv pip install --python $stagingPython --requirements $requirements --strict --index-strategy unsafe-best-match
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed. Current venv was not changed."
}

$versionJson = & $stagingPython -c "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'patch':sys.version_info.micro}))"
if ($LASTEXITCODE -ne 0) {
    throw "Staging Python could not run."
}
$version = $versionJson | ConvertFrom-Json
if ($version.major -ne 3 -or $version.minor -ne 11) {
    throw "Expected Python 3.11.x, received $($version.major).$($version.minor).$($version.patch)."
}

$pipCheck = & $stagingPython -m pip check 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "pip check failed: $(($pipCheck | Out-String).Trim())"
}

$backupVenv = $null
if (Test-Path -LiteralPath $currentVenv) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupVenv = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "venv.backup-$timestamp"))
    if (([System.IO.Path]::GetFullPath((Split-Path -Parent $backupVenv))) -ne $repoRoot) {
        throw "Unsafe backup path: $backupVenv"
    }
    if (Test-Path -LiteralPath $backupVenv) {
        throw "Backup destination already exists: $backupVenv"
    }
    Move-Item -LiteralPath $currentVenv -Destination $backupVenv
}

try {
    Move-Item -LiteralPath $stagingVenv -Destination $currentVenv
} catch {
    if ($null -ne $backupVenv -and -not (Test-Path -LiteralPath $currentVenv)) {
        Move-Item -LiteralPath $backupVenv -Destination $currentVenv
    }
    throw
}

$activePython = Join-Path $currentVenv "Scripts\python.exe"
Write-Output "Environment ready: $activePython"
Write-Output "Python: $(& $activePython --version)"
Write-Output "Profile: $Profile"
Write-Output "Dependencies: $(($pipCheck | Out-String).Trim())"
if ($null -ne $backupVenv) {
    Write-Output "Previous environment backup: $backupVenv"
}
