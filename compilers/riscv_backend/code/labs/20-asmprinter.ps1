# Chapter 20 lab: AsmPrinter output, attributes, CFI and symbol modifiers
$ErrorActionPreference = 'Continue'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlab20'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)

$mod = @(
    'target triple = "riscv64-unknown-elf"'
    '@g = global i64 7'
    'declare void @foo(i64)'
    'define i64 @plain() {'
    '  %v = load i64, ptr @g'
    '  ret i64 %v'
    '}'
    'define i64 @withv() #0 {'
    '  %v = load i64, ptr @g'
    '  ret i64 %v'
    '}'
    'define void @caller(i64 %x) {'
    '  call void @foo(i64 %x)'
    '  ret void'
    '}'
    'attributes #0 = { "target-features"="+m,+v" }'
) -join "`n"
[IO.File]::WriteAllText("$work\mod.ll", $mod, $enc)

Write-Host '== .attribute / .option arch / .cfi (asm-verbose on) ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 mod.ll -o -
Write-Host ''
Write-Host '== Same module with -asm-verbose=false ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 -asm-verbose=false mod.ll -o -
Write-Host ''
Write-Host '== Symbol modifiers: medlow vs medium ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 mod.ll -o - | Select-String -Pattern 'lui|ld|auipc|addi' | Select-Object -First 4
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 -code-model=medium mod.ll -o - | Select-String -Pattern 'lui|ld|auipc|addi|pcrel' | Select-Object -First 6
Write-Host ''
Write-Host '== .option norelax suppresses R_RISCV_RELAX on the call ==' -ForegroundColor Cyan
$asm = @(
    '.text'
    '.globl f'
    'f:'
    '  call foo'
    '  .option push'
    '  .option norelax'
    '  call bar'
    '  .option pop'
    '  ret'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\relax.s", $asm, $enc)
& "$bin\llvm-mc.exe" -triple=riscv64 -mattr=+relax -filetype=obj relax.s -o relax.o
& "$bin\llvm-readobj.exe" --relocations relax.o