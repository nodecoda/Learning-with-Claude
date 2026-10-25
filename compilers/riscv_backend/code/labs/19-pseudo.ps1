# Chapter 19 lab: pseudo expansion and machine passes
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlab19'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)
$ErrorActionPreference = 'Continue'

$scalar = @(
    'target triple = "riscv64-unknown-elf"'
    '@g = global i64 7'
    'declare void @foo()'
    'define ptr @addr() {'
    '  ret ptr @g'
    '}'
    'define void @tcall() {'
    '  tail call void @foo()'
    '  ret void'
    '}'
    'define i64 @loadg() {'
    '  %v = load i64, ptr @g'
    '  ret i64 %v'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\pseudo.ll", $scalar, $enc)

Write-Host '== After instruction selection (medium code model) ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -O2 -code-model=medium -stop-after=finalize-isel pseudo.ll -o - |
    Select-String -Pattern 'Pseudo|body:'

Write-Host ''
Write-Host '== riscv-prera-expand-pseudo: PseudoLLA -> AUIPC + ADDI ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -O2 -code-model=medium -print-after=riscv-prera-expand-pseudo pseudo.ll -o NUL 2>&1 |
    Select-String -Pattern 'AUIPC|ADDI|PseudoTAIL|PseudoRET' | Select-Object -First 8

Write-Host ''
Write-Host '== Final assembly (tail is still a pseudo in asm form) ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -O2 -code-model=medium pseudo.ll -o -
Get-Content "$work\pseudo.ll" | Out-Null

Write-Host ''
Write-Host '== Object file: tail becomes auipc + jalr, ret is jalr x0,0(ra) ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -O2 -code-model=medium -filetype=obj pseudo.ll -o pseudo.o
& "$bin\llvm-objdump.exe" -d pseudo.o

Write-Host ''
Write-Host '== Vector: PseudoVLE32_V_M1 before and after riscv-insert-vsetvli ==' -ForegroundColor Cyan
$vec = @(
    'target triple = "riscv64-unknown-elf"'
    'define void @addv4(ptr %a, ptr %b, ptr %c) {'
    '  %va = load <4 x i32>, ptr %a, align 16'
    '  %vb = load <4 x i32>, ptr %b, align 16'
    '  %s = add <4 x i32> %va, %vb'
    '  store <4 x i32> %s, ptr %c, align 16'
    '  ret void'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\vec.ll", $vec, $enc)

& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+v" -O2 "-riscv-v-vector-bits-min=128" -stop-after=finalize-isel vec.ll -o - |
    Select-String -Pattern 'PseudoV'
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+v" -O2 "-riscv-v-vector-bits-min=128" -print-before=riscv-insert-vsetvli vec.ll -o NUL 2>&1 |
    Select-String -Pattern 'PseudoV' | Select-Object -First 4
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+v" -O2 "-riscv-v-vector-bits-min=128" -print-after=riscv-insert-vsetvli vec.ll -o NUL 2>&1 |
    Select-String -Pattern 'PseudoV|VSET' | Select-Object -First 6
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+v" -O2 "-riscv-v-vector-bits-min=128" vec.ll -o - |
    Select-String -Pattern 'vsetivli|vle32|vadd|vse32'

Write-Host ''
Write-Host '== Atomic: pseudo AMO expands to a LR/SC loop without Zaamo ==' -ForegroundColor Cyan
$atomic = @(
    'target triple = "riscv64-unknown-elf"'
    'define i64 @atadd(ptr %p, i64 %v) {'
    '  %old = atomicrmw add ptr %p, i64 %v seq_cst'
    '  ret i64 %old'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\atomic.ll", $atomic, $enc)
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+zalrsc,-zaamo" -O2 -print-before=riscv-expand-atomic-pseudo atomic.ll -o NUL 2>&1 |
    Select-String -Pattern 'PseudoAtomic' | Select-Object -First 3
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+zalrsc,-zaamo" -O2 -print-after=riscv-expand-atomic-pseudo atomic.ll -o NUL 2>&1 |
    Select-String -Pattern 'LR_|SC_|BNE' | Select-Object -First 4
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+zalrsc,-zaamo" -O2 atomic.ll -o - |
    Select-String -Pattern 'lr\.|sc\.|bnez'