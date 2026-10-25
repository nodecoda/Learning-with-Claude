# Chapter 18 lab: scheduling model and llvm-mca on riscv64
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlab18'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)

# Marker files must end with a newline, otherwise llvm-mca mis-parses the END marker.
$regions = @(
    '# LLVM-MCA-BEGIN mul_add'
    'mul a0, a0, a1'
    'addi a0, a0, 7'
    '# LLVM-MCA-END'
    ''
    '# LLVM-MCA-BEGIN dep_add'
    'add a0, a0, a1'
    'add a0, a0, a1'
    'add a0, a0, a1'
    'add a0, a0, a1'
    '# LLVM-MCA-END'
    ''
    '# LLVM-MCA-BEGIN indep_add'
    'add a0, a0, a1'
    'add a2, a2, a3'
    'add a4, a4, a5'
    'add a6, a6, a7'
    '# LLVM-MCA-END'
    ''
    '# LLVM-MCA-BEGIN dep_mul'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    '# LLVM-MCA-END'
    ''
    '# LLVM-MCA-BEGIN load_use'
    'ld a1, 0(a0)'
    'add a1, a1, a2'
    'sd a1, 0(a3)'
    '# LLVM-MCA-END'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\regions.s", $regions, $enc)

Write-Host '== Summary: -mcpu=sifive-u74 ==' -ForegroundColor Cyan
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 regions.s |
    Select-String -Pattern 'Code Region|Total Cycles|IPC|Block RThroughput|Dispatch Width'

Write-Host ''
Write-Host '== Full report for mul_add ==' -ForegroundColor Cyan
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 regions.s | Select-Object -First 45

Write-Host ''
Write-Host '== Timeline for the dependent mul chain ==' -ForegroundColor Cyan
$chain = @(
    '# LLVM-MCA-BEGIN dep_mul'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    'mul a0, a0, a1'
    '# LLVM-MCA-END'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\dep_chain.s", $chain, $enc)
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 -iterations=1 -timeline dep_chain.s |
    Select-String -Pattern 'Timeline view' -Context 0,6

Write-Host ''
Write-Host '== Boundary: bottleneck analysis on an in-order CPU ==' -ForegroundColor Cyan
$ErrorActionPreference = 'Continue'
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 -bottleneck-analysis regions.s 2>&1 |
    Select-String -CaseSensitive -Pattern 'Data Dependencies|Resource Pressure|Register Dependencies' |
    Select-Object -First 6

Write-Host ''
Write-Host '== Boundary: no -mcpu, and a CPU without M ==' -ForegroundColor Cyan
& "$bin\llvm-mca.exe" -mtriple=riscv64 regions.s 2>&1 | Select-String -Pattern 'not a recognized processor' | Select-Object -First 2
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=generic-rv64 regions.s 2>&1 | Select-String -Pattern 'Zmmul|parse-failure' | Select-Object -First 3
$ErrorActionPreference = 'Stop'
& (Join-Path $bin 'llc.exe') --version | Out-Null
