# 14-legalization.ps1 -- legalization experiments (ASCII only)
$ErrorActionPreference = 'Stop'
$llvm = $env:LLVM_BIN
if (-not $llvm) { throw 'LLVM_BIN is not set; run through code/tools/run.ps1' }
$work = Join-Path $env:TEMP 'riscv-course-lab14'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Location $work

$legalLines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'define i64 @i64ops(i64 %a, i64 %b) {',
  '  %m = mul i64 %a, %b',
  '  %d = sdiv i64 %m, %b',
  '  %s = add i64 %m, %d',
  '  ret i64 %s',
  '}',
  'define half @hadd(half %a, half %b) {',
  '  %r = fadd half %a, %b',
  '  ret half %r',
  '}',
  'define double @dadd(double %a, double %b) {',
  '  %r = fadd double %a, %b',
  '  ret double %r',
  '}',
  'define <4 x i32> @vadd(<4 x i32> %a, <4 x i32> %b) {',
  '  %r = add <4 x i32> %a, %b',
  '  ret <4 x i32> %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'legal.ll'), $legalLines, (New-Object Text.UTF8Encoding($false)))

$i64Lines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'define i64 @i64add(i64 %a, i64 %b) {',
  '  %r = add i64 %a, %b',
  '  ret i64 %r',
  '}',
  'define i64 @i64mul(i64 %a, i64 %b) {',
  '  %r = mul i64 %a, %b',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'i64.ll'), $i64Lines, (New-Object Text.UTF8Encoding($false)))

$halfLines = @(
  'target triple = "riscv64-unknown-elf"',
  'define <vscale x 4 x half> @vhalf(<vscale x 4 x half> %a, <vscale x 4 x half> %b) {',
  '  %r = fadd <vscale x 4 x half> %a, %b',
  '  ret <vscale x 4 x half> %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'vhalf.ll'), $halfLines, (New-Object Text.UTF8Encoding($false)))

& (Join-Path $llvm 'llc.exe') -mtriple=riscv32 '-mattr=+m,+f,+d' -O2 legal.ll -o legal32.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+f,+d' -O2 legal.ll -o legal64.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=-d' -O2 legal.ll -o soft-d.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+d' -O2 legal.ll -o hard-d.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+v' -O2 legal.ll -o vec-v.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv32 '-mattr=+m' -O2 -global-isel i64.ll -o gisel32.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m' -O2 -global-isel i64.ll -o gisel64.s

Write-Host '=== RV32 i64 with +m ==='
Get-Content legal32.s | Select-String '^i64ops:' -Context 0,32
Write-Host '=== RV64 i64 with +m ==='
Get-Content legal64.s | Select-String '^i64ops:' -Context 0,10
Write-Host '=== soft double (-d) ==='
Get-Content soft-d.s | Select-String '^dadd:' -Context 0,9
Write-Host '=== hard double (+d) ==='
Get-Content hard-d.s | Select-String '^dadd:' -Context 0,6
Write-Host '=== fixed vector with +v ==='
Get-Content vec-v.s | Select-String '^vadd:' -Context 0,6
Write-Host '=== GlobalISel RV32 i64 ==='
Get-Content gisel32.s | Select-String '^i64add:' -Context 0,8
Get-Content gisel32.s | Select-String '^i64mul:' -Context 0,8
Write-Host '=== GlobalISel RV64 i64 ==='
Get-Content gisel64.s | Select-String '^i64add:' -Context 0,5

$ErrorActionPreference = 'Continue'
Write-Host '=== expected failure: scalable f16 without +zvfh ==='
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+v' -O2 vhalf.ll -o vhalf.s 2>&1 | Select-Object -First 4
Write-Host "failure exit code: $LASTEXITCODE"

Write-Host '=== expected failure: GlobalISel f16 without +zvfh ==='
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m' -O2 -global-isel legal.ll -o gisel-half.s 2>&1 | Select-Object -First 3
Write-Host "failure exit code: $LASTEXITCODE"
exit 0
