[CmdletBinding()]
param(
    [Parameter()]
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),

    [Parameter()]
    [string]$PythonPath = "",

    [Parameter()]
    [ValidateSet("Text", "Json")]
    [string]$OutputFormat = "Text",

    [Parameter()]
    [switch]$SkipCudaCheck,

    [Parameter()]
    [switch]$SkipLlamaHealth
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [ValidateSet("PASS", "FAIL", "SKIP")]
        [string]$Status,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $checks.Add([PSCustomObject]@{
        name = $Name
        status = $Status
        message = $Message
    })
}

function Resolve-ProjectPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ConfiguredPath
    )

    if ([System.IO.Path]::IsPathRooted($ConfiguredPath)) {
        return [System.IO.Path]::GetFullPath($ConfiguredPath)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $ConfiguredPath))
}

function Write-ResultAndExit {
    $failed = @($checks | Where-Object { $_.status -eq "FAIL" }).Count
    $passed = @($checks | Where-Object { $_.status -eq "PASS" }).Count
    $skipped = @($checks | Where-Object { $_.status -eq "SKIP" }).Count
    $result = [PSCustomObject]@{
        ok = ($failed -eq 0)
        summary = [PSCustomObject]@{
            passed = $passed
            failed = $failed
            skipped = $skipped
        }
        checks = $checks
    }

    if ($OutputFormat -eq "Json") {
        $result | ConvertTo-Json -Depth 5 -Compress
    } else {
        foreach ($check in $checks) {
            $symbol = switch ($check.status) {
                "PASS" { "[OK]" }
                "FAIL" { "[FAIL]" }
                default { "[SKIP]" }
            }
            Write-Output "$symbol $($check.name): $($check.message)"
        }
        Write-Output "Summary: $passed passed, $failed failed, $skipped skipped."
    }

    if ($failed -gt 0) {
        exit 1
    }
    exit 0
}

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    Add-Check -Name "project_root" -Status "PASS" -Message $ProjectRoot
} catch {
    Add-Check -Name "project_root" -Status "FAIL" -Message "Project root does not exist: $ProjectRoot"
    Write-ResultAndExit
}

$windowsVersion = [Environment]::OSVersion.Version
if ($env:OS -eq "Windows_NT" -and $windowsVersion.Build -ge 22000) {
    Add-Check -Name "windows" -Status "PASS" -Message "Windows build $($windowsVersion.Build)"
} else {
    Add-Check -Name "windows" -Status "FAIL" -Message "Windows 11 build >= 22000 is required; current version is $windowsVersion"
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonPath = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            $PythonPath = $pythonCommand.Source
        }
    }
}

$pythonReady = $false
if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    Add-Check -Name "python" -Status "FAIL" -Message "Python was not found. Install Python 3.11+ and recreate venv."
} else {
    try {
        $versionJson = & $PythonPath -c "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'patch':sys.version_info.micro,'text':sys.version.split()[0]}))"
        if ($LASTEXITCODE -ne 0) {
            throw "Python returned exit code $LASTEXITCODE"
        }
        $pythonVersion = $versionJson | ConvertFrom-Json
        if ($pythonVersion.major -eq 3 -and $pythonVersion.minor -ge 11) {
            Add-Check -Name "python" -Status "PASS" -Message "Python $($pythonVersion.text) at $PythonPath"
            $pythonReady = $true
        } else {
            Add-Check -Name "python" -Status "FAIL" -Message "Python 3.11+ is required; current version is $($pythonVersion.text)"
        }
    } catch {
        Add-Check -Name "python" -Status "FAIL" -Message "Python could not run at ${PythonPath}: $($_.Exception.Message)"
    }
}

$modelsConfigPath = Join-Path $ProjectRoot "config\models.yaml"
$modelConfig = $null
if (-not $pythonReady) {
    Add-Check -Name "models_config" -Status "FAIL" -Message "YAML cannot be checked until Python is ready."
}
elseif (-not (Test-Path -LiteralPath $modelsConfigPath -PathType Leaf)) {
    Add-Check -Name "models_config" -Status "FAIL" -Message "Missing config\models.yaml"
} else {
    try {
        $configReader = @'
import json
import pathlib
import sys
import yaml

try:
    data = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")) or {}
    llm = data.get("llm_main") or {}
    tts = data.get("tts") or {}
    required = ("binary", "model_path", "host", "port")
    missing = [key for key in required if key not in llm]
    if missing:
        raise ValueError("llm_main missing keys: " + ", ".join(missing))
    print(json.dumps({
        "ok": True,
        "config": {
            "binary": llm["binary"],
            "model_path": llm["model_path"],
            "host": llm["host"],
            "port": llm["port"],
            "reference_audio": tts.get("reference_audio"),
        },
    }))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
'@
        $configReaderBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($configReader))
        $configJson = & $PythonPath -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))' $configReaderBase64 $modelsConfigPath 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "YAML reader returned exit code ${LASTEXITCODE}: $(($configJson | Out-String).Trim())"
        }
        $configResult = $configJson | ConvertFrom-Json
        if (-not $configResult.ok) {
            throw ([string]$configResult.error)
        }
        $modelConfig = $configResult.config
        Add-Check -Name "models_config" -Status "PASS" -Message "config\models.yaml is valid"
    } catch {
        Add-Check -Name "models_config" -Status "FAIL" -Message "config\models.yaml could not be read: $($_.Exception.Message)"
    }
}

if ($null -ne $modelConfig) {
    $llamaBinary = Resolve-ProjectPath -ConfiguredPath ([string]$modelConfig.binary)
    if (Test-Path -LiteralPath $llamaBinary -PathType Leaf) {
        Add-Check -Name "llama_binary" -Status "PASS" -Message $llamaBinary
    } else {
        Add-Check -Name "llama_binary" -Status "FAIL" -Message "llama-server.exe was not found: $llamaBinary"
    }

    $llmModel = Resolve-ProjectPath -ConfiguredPath ([string]$modelConfig.model_path)
    if (Test-Path -LiteralPath $llmModel -PathType Leaf) {
        Add-Check -Name "llm_model" -Status "PASS" -Message $llmModel
    } else {
        Add-Check -Name "llm_model" -Status "FAIL" -Message "GGUF model was not found: $llmModel"
    }

    if ($null -ne $modelConfig.reference_audio -and -not [string]::IsNullOrWhiteSpace([string]$modelConfig.reference_audio)) {
        $referenceAudio = Resolve-ProjectPath -ConfiguredPath ([string]$modelConfig.reference_audio)
        if (Test-Path -LiteralPath $referenceAudio -PathType Leaf) {
            Add-Check -Name "tts_reference_audio" -Status "PASS" -Message $referenceAudio
        } else {
            Add-Check -Name "tts_reference_audio" -Status "FAIL" -Message "Reference audio was not found: $referenceAudio"
        }
    }

    if ($SkipLlamaHealth) {
        Add-Check -Name "llama_health" -Status "SKIP" -Message "Skipped by -SkipLlamaHealth"
    } else {
        $healthUri = "http://$($modelConfig.host):$($modelConfig.port)/health"
        try {
            $health = Invoke-RestMethod -Method Get -Uri $healthUri -TimeoutSec 5
            if ($health.status -eq "ok") {
                Add-Check -Name "llama_health" -Status "PASS" -Message $healthUri
            } else {
                Add-Check -Name "llama_health" -Status "FAIL" -Message "Health endpoint returned an invalid status: $healthUri"
            }
        } catch {
            Add-Check -Name "llama_health" -Status "FAIL" -Message "llama-server is not healthy at ${healthUri}: $($_.Exception.Message)"
        }
    }
}

if ($SkipCudaCheck) {
    Add-Check -Name "cuda" -Status "SKIP" -Message "Skipped by -SkipCudaCheck"
} else {
    try {
        $gpu = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "nvidia-smi returned exit code $LASTEXITCODE"
        }
        Add-Check -Name "cuda" -Status "PASS" -Message (($gpu | Select-Object -First 1) -as [string])
    } catch {
        Add-Check -Name "cuda" -Status "FAIL" -Message "NVIDIA driver/CUDA check failed: $($_.Exception.Message)"
    }
}

if ($pythonReady) {
    try {
        $pipOutput = & $PythonPath -m pip check 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw (($pipOutput | Out-String).Trim())
        }
        Add-Check -Name "dependencies" -Status "PASS" -Message (($pipOutput | Out-String).Trim())
    } catch {
        Add-Check -Name "dependencies" -Status "FAIL" -Message "pip check failed: $($_.Exception.Message)"
    }
} else {
    Add-Check -Name "dependencies" -Status "FAIL" -Message "pip check cannot run until Python is ready."
}

Write-ResultAndExit
