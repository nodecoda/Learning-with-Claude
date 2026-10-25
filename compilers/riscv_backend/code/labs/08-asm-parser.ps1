# Ch 08 lab: assembler aliases, pseudo expansion, errors, relocations.
# ASCII only (PowerShell 5.1 reads BOM-less UTF-8 as ANSI).
$ErrorActionPreference = 'Continue'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }
$mc = Join-Path $bin 'llvm-mc.exe'
$readobj = Join-Path $bin 'llvm-readobj.exe'

function Show-Case([string]$Title) {
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Invoke-McText {
    param([string]$Text, [string]$Name, [string[]]$Extra)
    $p = Join-Path $env:TEMP $Name
    [IO.File]::WriteAllText($p, $Text, (New-Object Text.UTF8Encoding($false)))
    & $mc -triple=riscv64 @Extra $p 2>&1
}

Show-Case 'A. alias and compressed echo'
$asmA = @'
jal  ra, 2048
jalr ra, 0(a0)
xori a0, a1, -1
mv   a0, a1
li   a0, -1
nop
j    16
c.addi a0, 1
c.lw a0, 8(a1)
'@
Invoke-McText $asmA 'riscv-ch08-a.asm' @('-mattr=+m,+a,+f,+d,+c', '-show-encoding')

Show-Case 'B. MCInst view'
$asmB = @'
addi a0, a1, 42
jal  ra, 2048
lw   a0, 8(sp)
'@
Invoke-McText $asmB 'riscv-ch08-b.asm' @('-mattr=+c', '-show-inst', '-show-encoding')

Show-Case 'C. li expansion'
$asmC = @'
li a0, 2048
li a0, -1
li a0, 0x123456789abcdef
'@
Invoke-McText $asmC 'riscv-ch08-c.asm' @('-mattr=+c', '-show-encoding')

Show-Case 'D. expected failures'
$bad = @(
    'add a0, a1, nope',
    'addi a0, a1, 4096',
    'beq a0, a1, 5000',
    'mul a0, a1, a2',
    'fadd.d fa0, fa1, fa2'
)
$i = 0
foreach ($c in $bad) {
    $i++
    Write-Host "-- $c"
    $o = Invoke-McText $c ("riscv-ch08-bad-{0}.asm" -f $i) @('-mattr=+c', '-show-encoding')
    foreach ($line in $o) { Write-Host ("OUT: " + $line.ToString()) }
    Write-Host "exit=$LASTEXITCODE"
}

Show-Case 'E. object file symbols and relocations'
$asmE = @'
.text
.globl foo
foo:
    ret
.globl use
use:
    call foo
    tail foo
    la   a0, g
    ret
.data
.globl g
.p2align 2
g:
    .word 42
'@
$asmPath = Join-Path $env:TEMP 'riscv-ch08-lab.asm'
$objPath = Join-Path $env:TEMP 'riscv-ch08-lab.o'
[IO.File]::WriteAllText($asmPath, $asmE, (New-Object Text.UTF8Encoding($false)))
& $mc -triple=riscv64 -filetype=obj $asmPath -o $objPath
Write-Host "assemble exit=$LASTEXITCODE"
& $readobj --symbols --relocations $objPath 2>&1
exit 0
