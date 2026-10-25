# 16-custom-lowering-intrinsics.ps1 -- intrinsic chain and TableGen validation (ASCII only)
$ErrorActionPreference = 'Continue'
$llvm = $env:LLVM_BIN
$inc = Join-Path (Split-Path -Parent $llvm) 'include'
if (-not $llvm) { throw 'LLVM_BIN is not set; run through code/tools/run.ps1' }
$work = Join-Path $env:TEMP 'riscv-course-lab16'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Location $work

$vfirst = @(
  'target triple = "riscv64-unknown-elf"',
  'declare i64 @llvm.riscv.vfirst.i64.nxv4i1(<vscale x 4 x i1>, i64)',
  'define i64 @vfirst(<vscale x 4 x i1> %m, i64 %vl) {',
  '  %r = call i64 @llvm.riscv.vfirst.i64.nxv4i1(<vscale x 4 x i1> %m, i64 %vl)',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'vfirst.ll'), $vfirst, (New-Object Text.UTF8Encoding($false)))

& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 '-mattr=+v' -O2 -print-after=riscv-isel vfirst.ll -o vfirst.s 2> vfirst-print.txt

Write-Host '=== IR intrinsic lowered to a pseudo ==='
Get-Content vfirst-print.txt | Select-String 'Machine code for function vfirst' -Context 0,10
Write-Host '=== final assembly ==='
Get-Content vfirst.s | Select-String '^vfirst:' -Context 0,8

$demo = @(
  'include "llvm/IR/Intrinsics.td"',
  '',
  'let TargetPrefix = "riscv" in {',
  '  def int_riscv_demo_absdiff : DefaultAttrsIntrinsic<[llvm_i64_ty],',
  '                                                     [llvm_i64_ty, llvm_i64_ty],',
  '                                                     [IntrNoMem, IntrSpeculatable]>;',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'DemoIntrinsics.td'), $demo, (New-Object Text.UTF8Encoding($false)))

Write-Host '=== TableGen accepts a new target intrinsic ==='
& (Join-Path $llvm 'llvm-tblgen.exe') -I $inc -gen-intrinsic-enums --intrinsic-prefix=riscv DemoIntrinsics.td -o DemoEnums.inc
Write-Host "tblgen exit code: $LASTEXITCODE"
Get-Content DemoEnums.inc | Select-String 'demo_absdiff'

Write-Host '=== existing RISCV intrinsic enum (generated header) ==='
$h = Join-Path $inc 'llvm\IR\IntrinsicsRISCV.h'
Get-Content $h | Select-String 'riscv_vfirst' | Select-Object -First 2