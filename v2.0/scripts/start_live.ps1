param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("youtube", "discord")]
    [string]$Platform,
    [string]$VideoId = "",
    [switch]$WithDiscord,
    [switch]$Memory,
    [switch]$NoMemory,
    [switch]$NoDashboard,
    [switch]$NoTts
)

$ErrorActionPreference = "Stop"
if ($Memory -and $NoMemory) {
    throw "-Memory and -NoMemory cannot be used together"
}
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot "venv\Scripts\python.exe"
$PreflightReport = Join-Path $RepoRoot "logs\operations\live_preflight.json"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python environment not found: $PythonExe"
}

$PreflightArgs = @(
    (Join-Path $PSScriptRoot "live_preflight.py"),
    "--platform", $Platform,
    "--skip-server-health",
    "--output", $PreflightReport
)
if ($VideoId) { $PreflightArgs += @("--video", $VideoId) }
if ($WithDiscord) { $PreflightArgs += "--with-discord" }
if (-not $NoDashboard) { $PreflightArgs += "--dashboard" }

& $PythonExe @PreflightArgs
if ($LASTEXITCODE -ne 0) {
    throw "Live preflight failed. Review $PreflightReport"
}

$RuntimeArgs = @()
if (-not $NoTts) { $RuntimeArgs += "--tts" }
if (-not $NoDashboard) { $RuntimeArgs += "--dashboard" }
if ($Memory) { $RuntimeArgs += "--memory" }
if ($NoMemory) { $RuntimeArgs += "--no-memory" }

Set-Location -LiteralPath $RepoRoot
if ($Platform -eq "youtube") {
    if (-not $VideoId) { throw "-VideoId is required for YouTube live" }
    $RuntimeArgs = @("--video", $VideoId) + $RuntimeArgs
    if ($WithDiscord) { $RuntimeArgs += "--with-discord" }
    & $PythonExe (Join-Path $PSScriptRoot "stream_youtube.py") @RuntimeArgs
} else {
    if ($VideoId) { $RuntimeArgs += @("--with-youtube", $VideoId) }
    & $PythonExe (Join-Path $PSScriptRoot "stream_discord.py") @RuntimeArgs
}

exit $LASTEXITCODE
