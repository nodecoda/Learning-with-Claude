# run.ps1 - single entry point for the TorchInductor deep-dive labs.
#
# Why this wrapper exists:
#   1. Inductor compiles C++ kernels with cl.exe on Windows, so MSVC env is required.
#   2. All experiments share one TORCHINDUCTOR_CACHE_DIR so artifacts are easy to inspect.
#   3. Everything runs with the course venv, not the system Python.
#
# Usage:
#   .\code\tools\run.ps1 code\labs\lab_a_pipeline.py
#   .\code\tools\run.ps1 code\labs\lab_b_pass.py -DebugArtifacts
#
# Override via env vars: VCVARS (vcvars64.bat), IND_PY (python.exe)

param(
    [Parameter(Mandatory = $true)][string]$Script,
    [switch]$DebugArtifacts
)

$ErrorActionPreference = "Stop"
$codeDir = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $codeDir "..\..\..")).Path

$vcvars = $env:VCVARS
if (-not $vcvars) { $vcvars = "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" }
if (-not (Test-Path -LiteralPath $vcvars)) {
    throw "vcvars64.bat not found: $vcvars. Set `$env:VCVARS."
}

$py = $env:IND_PY
if (-not $py) { $py = Join-Path $repoRoot ".scratch\aic-env\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $py)) { throw "python not found: $py. Set `$env:IND_PY." }

# Import vcvars into the current process instead of nesting cmd (avoids quote hell).
$envLines = & cmd /c "`"$vcvars`" >nul 2>&1 && set"
foreach ($line in $envLines) {
    if ($line -match '^([^=]+)=(.*)$') {
        Set-Item -Path ("env:" + $matches[1]) -Value $matches[2]
    }
}
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe still missing after vcvars. Check your MSVC installation."
}

$cacheDir = Join-Path $codeDir ".cache"
New-Item -ItemType Directory -Force $cacheDir | Out-Null
$env:TORCHINDUCTOR_CACHE_DIR = $cacheDir
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $codeDir
if ($DebugArtifacts) { $env:TORCH_COMPILE_DEBUG = "1" }

Write-Host "[run.ps1] python : $py"
Write-Host "[run.ps1] cache  : $cacheDir"
Write-Host "[run.ps1] script : $Script"
& $py $Script
exit $LASTEXITCODE