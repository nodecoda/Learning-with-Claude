# Practice D lab: llvm-mca comparison and a link audit
$ErrorActionPreference = 'Continue'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlabd'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)

$loopA = @(
    '# LLVM-MCA-BEGIN loop_a'
    'ld a1, 0(a0)'
    'add a1, a1, a2'
    'sd a1, 0(a3)'
    'addi a0, a0, 8'
    'addi a3, a3, 8'
    'addi a4, a4, -1'
    'bnez a4, .L_a'
    '# LLVM-MCA-END'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\loop_a.s", $loopA, $enc)

$loopB = @(
    '# LLVM-MCA-BEGIN loop_b'
    'ld a1, 0(a0)'
    'ld a5, 8(a0)'
    'add a1, a1, a2'
    'add a5, a5, a2'
    'sd a1, 0(a3)'
    'sd a5, 8(a3)'
    'addi a0, a0, 16'
    'addi a3, a3, 16'
    'addi a4, a4, -2'
    'bnez a4, .L_b'
    '# LLVM-MCA-END'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\loop_b.s", $loopB, $enc)

$loopC = @(
    '# LLVM-MCA-BEGIN loop_c'
    'ld a1, 0(a0)'
    'ld a5, 8(a0)'
    'ld a6, 16(a0)'
    'ld a7, 24(a0)'
    'add a1, a1, a2'
    'add a5, a5, a2'
    'add a6, a6, a2'
    'add a7, a7, a2'
    'sd a1, 0(a3)'
    'sd a5, 8(a3)'
    'sd a6, 16(a3)'
    'sd a7, 24(a3)'
    'addi a0, a0, 32'
    'addi a3, a3, 32'
    'addi a4, a4, -4'
    'bnez a4, .L_c'
    '# LLVM-MCA-END'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\loop_c.s", $loopC, $enc)

Write-Host '== llvm-mca: three loop bodies (sifive-u74) ==' -ForegroundColor Cyan
foreach ($f in 'loop_a', 'loop_b', 'loop_c') {
    Write-Host ('--- ' + $f + ' ---')
    & "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 ("$f.s") |
        Select-String -CaseSensitive -Pattern '^Instructions:|^Total Cycles|^IPC|^Block RThroughput'
}

Write-Host ''
Write-Host '== Resource pressure of loop_a ==' -ForegroundColor Cyan
& "$bin\llvm-mca.exe" -mtriple=riscv64 -mcpu=sifive-u74 loop_a.s |
    Select-String -Pattern 'Resource pressure per iteration' -Context 0,3

Write-Host ''
Write-Host '== Link audit: global variable, HI20/LO12 ==' -ForegroundColor Cyan
$glob = @(
    'target triple = "riscv64-unknown-elf"'
    '@g = global i64 42, align 8'
    'define i64 @get_g() {'
    '  %v = load i64, ptr @g, align 8'
    '  ret i64 %v'
    '}'
    'define void @set_g(i64 %v) {'
    '  store i64 %v, ptr @g, align 8'
    '  ret void'
    '}'
    'define ptr @addr_g() {'
    '  ret ptr @g'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\glob.ll", $glob, $enc)

& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 -filetype=obj glob.ll -o glob.o
Write-Host '--- relocations in glob.o ---'
& "$bin\llvm-readobj.exe" --relocations glob.o | Select-String -Pattern 'R_RISCV'
Write-Host '--- disassembly in glob.o (immediates still zero) ---'
& "$bin\llvm-objdump.exe" -d glob.o

& "$bin\ld.lld.exe" -o glob.elf glob.o -e get_g
Write-Host '--- disassembly after ld.lld (immediates resolved) ---'
& "$bin\llvm-objdump.exe" -d --section=.text glob.elf
Write-Host '--- .data address and relocations after link ---'
& "$bin\llvm-readobj.exe" --sections glob.elf | Select-String -Pattern 'Name: .data|Address: 0x122'
& "$bin\llvm-readobj.exe" --relocations glob.elf

Write-Host ''
Write-Host '== Relaxation: call foo becomes jal ==' -ForegroundColor Cyan
$relax = @(
    '.text'
    '.globl _start'
    '_start:'
    '  call foo'
    '  ret'
    '.globl foo'
    'foo:'
    '  ret'
    ''
) -join "`n"
[IO.File]::WriteAllText("$work\link.s", $relax, $enc)
& "$bin\llvm-mc.exe" -triple=riscv64 -mattr=+relax -filetype=obj link.s -o link.o
& "$bin\llvm-readobj.exe" --relocations link.o | Select-String -Pattern 'R_RISCV'
& "$bin\ld.lld.exe" -o link.elf link.o -e _start
& "$bin\llvm-objdump.exe" -d link.elf