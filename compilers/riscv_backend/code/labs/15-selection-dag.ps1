# 15-selection-dag.ps1 -- SelectionDAG evidence (ASCII only)
$ErrorActionPreference = 'Continue'
$llvm = $env:LLVM_BIN
if (-not $llvm) { throw 'LLVM_BIN is not set; run through code/tools/run.ps1' }
$work = Join-Path $env:TEMP 'riscv-course-lab15'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Location $work

$lines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'define i64 @mul_add(i64 %a, i64 %b) {',
  'entry:',
  '  %m = mul i64 %a, %b',
  '  %r = add i64 %m, 7',
  '  ret i64 %r',
  '}',
  'define signext i32 @sel(i32 signext %a, i32 signext %b, i1 %c) {',
  'entry:',
  '  %s = select i1 %c, i32 %a, i32 %b',
  '  ret i32 %s',
  '}',
  'define i64 @ldst(ptr %p, i64 %v) {',
  'entry:',
  '  store i64 %v, ptr %p, align 8',
  '  %x = load i64, ptr %p, align 8',
  '  ret i64 %x',
  '}',
  'define i64 @caller(i64 %a) {',
  'entry:',
  '  %r = call i64 @mul_add(i64 %a, i64 3)',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'basic.ll'), $lines, (New-Object Text.UTF8Encoding($false)))

& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=riscv-isel -simplify-mir basic.ll -o after-isel.mir
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=finalize-isel -simplify-mir basic.ll -o final-isel.mir
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=finalize-isel basic.ll -o final-full.mir
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O0 basic.ll -o O0.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 basic.ll -o O2.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -print-after=riscv-isel basic.ll -o NUL 2> isel-print.txt

Write-Host '=== after riscv-isel: select pseudo ==='
Get-Content after-isel.mir | Select-String 'name: *sel' -Context 0,31
Write-Host '=== after finalize-isel: select expanded ==='
Get-Content final-isel.mir | Select-String 'name: *sel' -Context 0,31
Write-Host '=== MIR size with and without -simplify-mir ==='
$full = Get-Item final-full.mir
$simple = Get-Item final-isel.mir
Write-Host ("full bytes={0}, simplified bytes={1}" -f $full.Length, $simple.Length)
Write-Host '=== -O0 sel ==='
Get-Content O0.s | Select-String '^sel:' -Context 0,18
Write-Host '=== -O2 sel ==='
Get-Content O2.s | Select-String '^sel:' -Context 0,10
Write-Host '=== print-after=riscv-isel: fp constant ==='
Get-Content isel-print.txt | Select-String 'Machine code for function mul_add' -Context 0,10

Write-Host '=== boundary: -debug-only is unavailable in the optimized build ==='
$ErrorActionPreference = 'Continue'
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m' -O2 -debug-only=isel basic.ll -o NUL 2>&1 | Select-Object -First 2
Write-Host "exit code: $LASTEXITCODE"
exit 0
