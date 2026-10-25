# Ch 10 lab: MC layer, sections, attributes, .option, alignment.
# ASCII only.
param([string]$Case = 'ALL')
if ($Case -eq 'ALL') {
    foreach ($c in 'A','B','C','D','E') {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Case $c
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    exit 0
}
$ErrorActionPreference = 'Continue'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }
$llc = Join-Path $bin 'llc.exe'
$mc = Join-Path $bin 'llvm-mc.exe'
$readobj = Join-Path $bin 'llvm-readobj.exe'
$readelf = Join-Path $bin 'llvm-readelf.exe'
$dir = Join-Path $env:TEMP 'riscv-ch10'
New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Show-Case([string]$Title) {
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor Cyan
}
function Show-Out($Items) {
    foreach ($line in $Items) { Write-Host ("OUT: " + $line.ToString()) }
}
function Write-Ascii([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

$irA = @'
@.str = private unnamed_addr constant [6 x i8] c"hello\00", align 1
@g = dso_local global i32 42, align 4
declare i32 @ext(i32)
declare i32 @puts(ptr)

define dso_local i32 @f(i32 %x) {
entry:
  %y = call i32 @ext(i32 %x)
  ret i32 %y
}

define dso_local i32 @h() {
entry:
  %p = call i32 @puts(ptr @.str)
  %v = load i32, ptr @g
  ret i32 %v
}
'@

$plain = @'
	.text
	.globl f
f:
	addi a0, a1, 1
	ret
'@

$attr = @'
	.attribute 5, "rv64i2p1_m2p0"
	.text
	.globl f
f:
	mul a0, a0, a1
	ret
'@

$opt1 = @'
	.text
opt1:
	.option arch, +m
	mul a0, a0, a1
	.option push
	.option arch, +c
	lw a0, 8(sp)
	.option pop
	lw a0, 8(sp)
	.option norvc
	lw a0, 8(sp)
	.option rvc
	lw a0, 8(sp)
	ret
'@

$align = @'
	.data
	.byte 1
	.align 3
	.byte 2
	.globl g1
g1:
	.word 7
	.text
	.globl f
f:
	addi a0, a0, 1
	.size f, .-f
'@

$bss = @'
	.bss
	.align 4
	.globl b1
b1:
	.zero 8
'@

$minusM = @'
	.text
	.option arch, +m
	.option arch, -m
	div a0, a0, a1
	mul a0, a0, a1
'@

$setArch = @'
	.text
	.option arch, rv64i
	mul a0, a0, a1
'@

switch ($Case) {
    'A' {
        Show-Case 'A. IR to object: sections, symbols, attributes'
        $ll = Join-Path $dir 'ch10.ll'
        $obj = Join-Path $dir 'ch10.o'
        Write-Ascii $ll $irA
        $o = & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -filetype=obj $ll -o $obj 2>&1
        Show-Out $o
        Write-Host "llc exit=$LASTEXITCODE"
        Show-Out (& $readobj --sections $obj 2>&1)
        Show-Out (& $readobj --symbols $obj 2>&1)
        Show-Out (& $readelf -A $obj 2>&1)
    }
    'B' {
        Show-Case 'B. minimal llvm-mc object vs .attribute'
        $p1 = Join-Path $dir 'plain.s'
        $p2 = Join-Path $dir 'attr.s'
        $o1 = Join-Path $dir 'plain.o'
        $o2 = Join-Path $dir 'attr.o'
        Write-Ascii $p1 $plain
        Write-Ascii $p2 $attr
        & $mc -triple=riscv64 -filetype=obj $p1 -o $o1
        & $mc -triple=riscv64 -filetype=obj $p2 -o $o2
        Write-Host "plain exit=$LASTEXITCODE"
        Write-Host '-- plain sections'
        Show-Out (& $readobj --sections $o1 2>&1 | Select-String 'Name:|Type:|Size:|AddressAlignment:|EntrySize:')
        Write-Host '-- attr sections'
        Show-Out (& $readobj --sections $o2 2>&1 | Select-String 'Name:|Type:|Size:|AddressAlignment:|EntrySize:')
        Write-Host '-- attr build attributes'
        Show-Out (& $readelf -A $o2 2>&1)
    }
    'C' {
        Show-Case 'C. .option arch/push/pop/norvc/rvc'
        $src = Join-Path $dir 'opt1.s'
        Write-Ascii $src $opt1
        Write-Host '-- no -mattr'
        Show-Out (& $mc -triple=riscv64 -show-encoding $src 2>&1)
        Write-Host "exit=$LASTEXITCODE"
        Write-Host '-- -mattr=+c'
        Show-Out (& $mc -triple=riscv64 '-mattr=+c' -show-encoding $src 2>&1)
        Write-Host "exit=$LASTEXITCODE"
    }
    'D' {
        Show-Case 'D. alignment, padding, .align as power of two'
        $a = Join-Path $dir 'align.s'
        $b = Join-Path $dir 'bss.s'
        $ao = Join-Path $dir 'align.o'
        $bo = Join-Path $dir 'bss.o'
        Write-Ascii $a $align
        Write-Ascii $b $bss
        & $mc -triple=riscv64 -filetype=obj $a -o $ao
        & $mc -triple=riscv64 -filetype=obj $b -o $bo
        Write-Host "mc exit=$LASTEXITCODE"
        Write-Host '-- align.o'
        Show-Out (& $readobj --sections $ao 2>&1 | Select-String 'Name:|Offset:|Size:|AddressAlignment:|EntrySize:')
        Write-Host '-- g1 symbol'
        Show-Out (& $readobj --symbols $ao 2>&1 | Select-String 'Name: g1|Value:|Size:|Section:')
        Write-Host '-- bss.o'
        Show-Out (& $readobj --sections $bo 2>&1 | Select-String 'Name:|Size:|AddressAlignment:')
    }
    'E' {
        Show-Case 'E. failure and boundary cases'
        $p1 = Join-Path $dir 'minus-m.s'
        $p2 = Join-Path $dir 'setarch.s'
        Write-Ascii $p1 $minusM
        Write-Ascii $p2 $setArch
        Write-Host '-- .option arch, -m'
        Show-Out (& $mc -triple=riscv64 -show-encoding $p1 2>&1)
        Write-Host "exit=$LASTEXITCODE"
        Write-Host '-- .option arch, rv64i'
        Show-Out (& $mc -triple=riscv64 -show-encoding $p2 2>&1)
        Write-Host "exit=$LASTEXITCODE"
        Write-Host '-- llvm-readobj --attributes does not exist'
        $obj = Join-Path $dir 'plain.o'
        if (-not (Test-Path $obj)) {
            Write-Ascii (Join-Path $dir 'plain.s') $plain
            & $mc -triple=riscv64 -filetype=obj (Join-Path $dir 'plain.s') -o $obj
        }
        Show-Out (& $readobj --attributes $obj 2>&1)
        Write-Host "exit=$LASTEXITCODE"
    }
    default {
        Write-Host 'Usage: run.ps1 labs\10-mc-layer.ps1 -Case A|B|C|D|E|ALL'
    }
}
exit 0
