# Chapter 21 lab: relocations, code models, RELAX and ld.lld
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlab21'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)
$ErrorActionPreference = 'Continue'

$glob = @(
    'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
    'target triple = "riscv64-unknown-elf"'
    ''
    '@g = global i64 42, align 8'
    ''
    'define i64 @get_g() {'
    'entry:'
    '  %v = load i64, ptr @g, align 8'
    '  ret i64 %v'
    '}'
    ''
    'define void @set_g(i64 %v) {'
    'entry:'
    '  store i64 %v, ptr @g, align 8'
    '  ret void'
    '}'
    ''
    'define ptr @addr_g() {'
    'entry:'
    '  ret ptr @g'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\glob.ll", $glob, $enc)

Write-Host '== A. small / medium / large: relocations ==' -ForegroundColor Cyan
foreach ($cm in 'small', 'medium', 'large') {
    Write-Host "--- code-model=$cm ---" -ForegroundColor Yellow
    & "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 "-code-model=$cm" -filetype=obj glob.ll -o "glob-$cm.o"
    & "$bin\llvm-readelf.exe" -r "glob-$cm.o" | Select-String -Pattern 'R_RISCV'
}

Write-Host ''
Write-Host '== A. small / medium / large: assembly ==' -ForegroundColor Cyan
foreach ($cm in 'small', 'medium', 'large') {
    Write-Host "--- code-model=$cm ---" -ForegroundColor Yellow
    & "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 "-code-model=$cm" glob.ll -o "glob-$cm.s"
    Get-Content "glob-$cm.s" | Select-String -Pattern 'auipc|lui|ld|addi|sd|\.quad|pcrel|LCPI'
}

Write-Host ''
Write-Host '== B. -relocation-model=pic: GOT_HI20 ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -relocation-model=pic -filetype=obj glob.ll -o glob-pic.o
& "$bin\llvm-readelf.exe" -r glob-pic.o | Select-String -Pattern 'R_RISCV'
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -relocation-model=pic glob.ll -o glob-pic.s
Get-Content glob-pic.s | Select-String -Pattern 'auipc|ld|got_pcrel|pcrel_lo'

Write-Host ''
Write-Host '== C. call foo -> jal foo after ld.lld ==' -ForegroundColor Cyan
$link = @(
    '.text'
    '.globl _start'
    '_start:'
    '  call foo'
    '  ret'
    '.globl foo'
    'foo:'
    '  li a0, 7'
    '  ret'
) -join "`n"
[IO.File]::WriteAllText("$work\link.s", $link, $enc)

& "$bin\llvm-mc.exe" -triple=riscv64 '-mattr=+relax' -filetype=obj link.s -o link-relax.o
& "$bin\llvm-readelf.exe" -r link-relax.o
& "$bin\ld.lld.exe" -o link-relax.elf link-relax.o
Write-Host '--- relaxed ---' -ForegroundColor Yellow
& "$bin\llvm-objdump.exe" -d link-relax.elf | Select-String -Pattern '11120|11124|11128|1112c|jal|auipc|jalr|ret|foo'
& "$bin\ld.lld.exe" --no-relax -o link-norelax.elf link-relax.o
Write-Host '--- --no-relax ---' -ForegroundColor Yellow
& "$bin\llvm-objdump.exe" -d link-norelax.elf | Select-String -Pattern '11120|11124|11128|1112c|11130|jal|auipc|jalr|ret|foo'

Write-Host ''
Write-Host '== D. +relax also adds R_RISCV_ALIGN and ADD32/SUB32 ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d,+relax' -O2 -filetype=obj glob.ll -o glob-relax.o
& "$bin\llvm-readelf.exe" -r glob-relax.o | Select-String -Pattern 'ALIGN|ADD32|SUB32|HI20|LO12'

Write-Host ''
Write-Host '== E. failure: R_RISCV_HI20 out of range ==' -ForegroundColor Cyan
$hi = @(
    '.text'
    '.globl _start'
    '_start:'
    '  lui a0, %hi(g)'
    '  ld a0, %lo(g)(a0)'
    '  ret'
) -join "`n"
[IO.File]::WriteAllText("$work\hi20.s", $hi, $enc)
& "$bin\llvm-mc.exe" -triple=riscv64 -filetype=obj hi20.s -o hi20.o
& cmd.exe /c "`"$bin\ld.lld.exe`" --defsym=g=0x80000000 -o hi20-bad.elf hi20.o 2>&1" | Select-Object -First 4

Write-Host ''
Write-Host '== F. boundary: %hi rounds up, %lo compensates ==' -ForegroundColor Cyan
& cmd.exe /c "`"$bin\ld.lld.exe`" --defsym=g=0x12345abc -o hi20-round.elf hi20.o 2>&1" | Out-Null
& "$bin\llvm-objdump.exe" -d hi20-round.elf
& "$bin\llvm-nm.exe" -n hi20-round.elf

Write-Host ''
Write-Host '== G. failure: R_RISCV_PCREL_HI20 out of range ==' -ForegroundColor Cyan
& cmd.exe /c "`"$bin\ld.lld.exe`" --defsym=g=0x90000000 -o medium-bad.elf glob-medium.o 2>&1" | Select-Object -First 6
exit 0
