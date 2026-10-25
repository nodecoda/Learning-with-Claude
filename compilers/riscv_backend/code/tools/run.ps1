# compilers/riscv_backend/code/tools/run.ps1
# Unified entry: prepend the LLVM 23.1.4 bin to PATH, then run a lab script.
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)][string]$Script,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'

$toolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$codeDir   = Split-Path -Parent $toolsDir
$courseDir = Split-Path -Parent $codeDir
$repoDir   = Split-Path -Parent (Split-Path -Parent $courseDir)

$llvmBin = Join-Path $repoDir '.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
if (-not (Test-Path (Join-Path $llvmBin 'llc.exe'))) {
    throw "LLVM 23.1.4 not found at: $llvmBin"
}

$env:PATH = "$llvmBin;$env:PATH"
$env:LLVM_BIN = $llvmBin
$env:LLVM_SRC_TD = Join-Path $repoDir '.scratch\llvm-23.1.4-riscv-td\llvm\lib\Target\RISCV'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if ([IO.Path]::IsPathRooted($Script)) {
    $resolved = $Script
} else {
    $resolved = Join-Path (Join-Path $courseDir 'code') $Script
}

if (-not (Test-Path $resolved)) { throw "Script not found: $resolved" }

$ver = (& (Join-Path $llvmBin 'llc.exe') --version) | Select-Object -First 2
Write-Host "[run] $($ver -join ' | ')" -ForegroundColor DarkGray

switch -Wildcard ($resolved) {
    '*.ps1' { & powershell -NoProfile -ExecutionPolicy Bypass -File $resolved @Rest; break }
    '*.py'  { & python $resolved @Rest; break }
    default { & $resolved @Rest }
}

if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }