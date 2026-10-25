# 13-frame-lowering.ps1 -- stack frame experiments (ASCII only)
$ErrorActionPreference = 'Stop'
$llvm = $env:LLVM_BIN
if (-not $llvm) { throw 'LLVM_BIN is not set; run through code/tools/run.ps1' }
$work = Join-Path $env:TEMP 'riscv-course-lab13'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Location $work

$basicLines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'define i64 @mul_add(i64 %a, i64 %b) {',
  'entry:',
  '  %m = mul i64 %a, %b',
  '  %r = add i64 %m, 7',
  '  ret i64 %r',
  '}',
  'define i64 @caller(i64 %a) {',
  'entry:',
  '  %r = call i64 @mul_add(i64 %a, i64 3)',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'basic.ll'), $basicLines, (New-Object Text.UTF8Encoding($false)))

$frameLines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'declare i64 @use(ptr)',
  'define i64 @big(i64 %a) {',
  'entry:',
  '  %buf = alloca [5000 x i8], align 1',
  '  %r = call i64 @use(ptr %buf)',
  '  %s = add i64 %r, %a',
  '  ret i64 %s',
  '}',
  'define i64 @huge(i64 %a) {',
  'entry:',
  '  %buf = alloca [12000 x i8], align 16',
  '  %r = call i64 @use(ptr %buf)',
  '  ret i64 %r',
  '}',
  'define i64 @dyn(i64 %n) {',
  'entry:',
  '  %buf = alloca i8, i64 %n, align 16',
  '  %r = call i64 @use(ptr %buf)',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'frame.ll'), $frameLines, (New-Object Text.UTF8Encoding($false)))

$rvvLines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  'declare void @usev(ptr)',
  'define void @rvv(i64 %n) {',
  'entry:',
  '  %buf = alloca <vscale x 4 x i32>, align 16',
  '  call void @usev(ptr %buf)',
  '  ret void',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'rvvframe.ll'), $rvvLines, (New-Object Text.UTF8Encoding($false)))

& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 basic.ll -o caller-default.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -frame-pointer=all basic.ll -o caller-fp-all.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -frame-pointer=none basic.ll -o caller-fp-none.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 -O2 frame.ll -o frame.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 -O2 -frame-pointer=none frame.ll -o frame-none.s
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+v' -O2 rvvframe.ll -o rvv.s

Write-Host '=== caller default ==='
Get-Content caller-default.s | Select-String '^caller:' -Context 0,14
Write-Host '=== caller -frame-pointer=all ==='
Get-Content caller-fp-all.s | Select-String '^caller:' -Context 0,20
Write-Host '=== big (5000-byte local) ==='
Get-Content frame.s | Select-String '^big:' -Context 0,22
Write-Host '=== huge (12000-byte local) ==='
Get-Content frame.s | Select-String '^huge:' -Context 0,18
Write-Host '=== dyn with -frame-pointer=none ==='
Get-Content frame-none.s | Select-String '^dyn:' -Context 0,16
Write-Host '=== RVV scalable object ==='
Get-Content rvv.s | Select-String '^rvv:' -Context 0,18