# Ch 11 lab: registers, classes, MIR vregs, spills, zero register.
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
$dir = Join-Path $env:TEMP 'riscv-ch11'
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

$mixIr = @'
define dso_local double @mix(i64 %a, i64 %b, double %x, float %y) {
entry:
  %m = mul i64 %a, %b
  %i = sitofp i64 %m to double
  %f = fadd double %i, %x
  %y2 = fpext float %y to double
  %r = fadd double %f, %y2
  %u = fptoui double %r to i64
  %d = uitofp i64 %u to double
  ret double %d
}
'@

$pressureIr = @'
declare i64 @ext8(i64, i64, i64, i64, i64, i64, i64, i64)

define dso_local i64 @pressure2(i64 %a0, i64 %a1, i64 %a2, i64 %a3, i64 %a4, i64 %a5, i64 %a6, i64 %a7) {
entry:
  %s0 = mul i64 %a0, %a1
  %s1 = mul i64 %a2, %a3
  %s2 = mul i64 %a4, %a5
  %s3 = mul i64 %a6, %a7
  %s4 = add i64 %s0, %s1
  %s5 = add i64 %s2, %s3
  %s6 = xor i64 %s4, %s5
  %s7 = sub i64 %s4, %s5
  %c = call i64 @ext8(i64 %s0, i64 %s1, i64 %s2, i64 %s3, i64 %s4, i64 %s5, i64 %s6, i64 %s7)
  %t0 = add i64 %c, %s0
  %t1 = add i64 %t0, %s1
  %t2 = add i64 %t1, %s2
  %t3 = add i64 %t2, %s3
  %t4 = add i64 %t3, %s6
  %t5 = add i64 %t4, %s7
  ret i64 %t5
}
'@

$mix = Join-Path $dir 'mix.ll'
$pressure = Join-Path $dir 'pressure2.ll'
Write-Ascii $mix $mixIr
Write-Ascii $pressure $pressureIr

switch ($Case) {
    'A' {
        Show-Case 'A. finalize-isel: vregs and classes'
        $mir = Join-Path $dir 'mix-isel.mir'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=finalize-isel $mix -o $mir
        Write-Host "llc exit=$LASTEXITCODE"
        Show-Out (Select-String -Path $mir -Pattern '^noVRegs:|^registers:|^liveins:|^  - \{ id:|^\s+liveins:.*\$|MUL|FCVT|FADD' | ForEach-Object { $_.Line })
    }
    'B' {
        Show-Case 'B. postrapseudos: physical registers, then final asm'
        $mir = Join-Path $dir 'mix-postra.mir'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=postrapseudos $mix -o $mir
        Write-Host "llc exit=$LASTEXITCODE"
        Show-Out (Select-String -Path $mir -Pattern '^noVRegs:|MUL|FCVT|FADD|FMV' | ForEach-Object { $_.Line })
        Write-Host '-- final asm'
        Show-Out (& $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 $mix -o - 2>&1 | Select-String 'mul|fcvt|fadd|ret')
    }
    'C' {
        Show-Case 'C. register pressure and callee-saved spills'
        Write-Host '-- final asm'
        Show-Out (& $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 $pressure -o - 2>&1 | Select-String 'addi\s+sp|sd\s+|ld\s+|call|mul|add\s+s|xor|sub|mv\s+')
        Write-Host '-- postrapseudos frame'
        $mir = Join-Path $dir 'pressure-postra.mir'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=postrapseudos $pressure -o $mir
        Show-Out (Select-String -Path $mir -Pattern '^noVRegs:|stackSize|frame-setup|frame-destroy' | ForEach-Object { $_.Line })
    }
    'D' {
        Show-Case 'D. zero register and ABI names'
        Write-Host '-- llvm-mc echo'
        $zsrc = Join-Path $dir 'zero.s'
        Write-Ascii $zsrc "`tadd a0, zero, a1`n`taddi x0, x0, 1`n`tsd x8, 0(sp)`n"
        Show-Out (& $mc -triple=riscv64 '-mattr=+c' -show-encoding $zsrc 2>&1)
        Write-Host "exit=$LASTEXITCODE"
        Write-Host '-- ret i64 0'
        $z = Join-Path $dir 'zero.ll'
        Write-Ascii $z 'define i64 @zero_ret() { ret i64 0 }'
        Show-Out (& $llc -mtriple=riscv64 -O2 $z -o - 2>&1 | Select-String 'li|ret')
    }
    'E' {
        Show-Case 'E. pass-name boundaries'
        $g = Join-Path $dir 'mix-greedy.mir'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=greedy $mix -o $g
        Write-Host "greedy exit=$LASTEXITCODE"
        Show-Out (Select-String -Path $g -Pattern '^noVRegs:|^registers:' | ForEach-Object { $_.Line })
        Show-Out (Select-String -Path $g -Pattern '^{ id: 0|  - \{ id: 0' | ForEach-Object { $_.Line })
        Write-Host '-- prologepilog is not a registered stop name'
        Show-Out (& $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 -stop-after=prologepilog $mix -o (Join-Path $dir 'never.mir') 2>&1 | Select-Object -First 2)
        Write-Host "exit=$LASTEXITCODE"
    }
    default {
        Write-Host 'Usage: run.ps1 labs\11-registers.ps1 -Case A|B|C|D|E|ALL'
    }
}
exit 0
