# Ch 12 lab: RISC-V calling convention, ABI, varargs, sret, byval.
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
$objdump = Join-Path $bin 'llvm-objdump.exe'
$readelf = Join-Path $bin 'llvm-readelf.exe'
$ld = Join-Path $bin 'ld.lld.exe'
$dir = Join-Path $env:TEMP 'riscv-ch12'
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
function Show-Func([string]$Path, [string]$Name) {
    $s = Get-Content $Path -Encoding UTF8
    $hit = $s | Select-String ('^' + [regex]::Escape($Name) + ':')
    if (-not $hit) { Write-Host ("MISSING FUNCTION: " + $Name); return }
    $i = $hit[0].LineNumber
    $end = $s | Select-String '\.Lfunc_end' | Where-Object { $_.LineNumber -gt $i } | Select-Object -First 1
    if ($end) { Show-Out $s[($i - 1)..($end.LineNumber + 1)] } else { Show-Out $s[($i - 1)..($s.Count - 1)] }
}

$abiIr = @'
define i64 @sum9(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e, i64 %f, i64 %g, i64 %h, i64 %i) {
  %s1 = add i64 %a, %b
  %s2 = add i64 %s1, %c
  %s3 = add i64 %s2, %d
  %s4 = add i64 %s3, %e
  %s5 = add i64 %s4, %f
  %s6 = add i64 %s5, %g
  %s7 = add i64 %s6, %h
  %s8 = add i64 %s7, %i
  ret i64 %s8
}
define double @sumd9(double %a, double %b, double %c, double %d, double %e, double %f, double %g, double %h, double %i) {
  %s1 = fadd double %a, %b
  %s2 = fadd double %s1, %c
  %s3 = fadd double %s2, %d
  %s4 = fadd double %s3, %e
  %s5 = fadd double %s4, %f
  %s6 = fadd double %s5, %g
  %s7 = fadd double %s6, %h
  %s8 = fadd double %s7, %i
  ret double %s8
}
define float @sumf9(float %a, float %b, float %c, float %d, float %e, float %f, float %g, float %h, float %i) {
  %s1 = fadd float %a, %b
  %s2 = fadd float %s1, %c
  %s3 = fadd float %s2, %d
  %s4 = fadd float %s3, %e
  %s5 = fadd float %s4, %f
  %s6 = fadd float %s5, %g
  %s7 = fadd float %s6, %h
  %s8 = fadd float %s7, %i
  ret float %s8
}
define double @mixed(i64 %a, double %x, i64 %b, double %y) {
  %ai = sitofp i64 %a to double
  %bi = sitofp i64 %b to double
  %s1 = fadd double %ai, %x
  %s2 = fadd double %bi, %y
  %r = fadd double %s1, %s2
  ret double %r
}
'@

$varIr = @'
declare double @vsum(i32, ...)
define double @call_vsum() {
  %r = call double (i32, ...) @vsum(i32 1, double 1.5)
  ret double %r
}
define i64 @vararg(i32 %n, ...) {
  ret i64 0
}
'@

$var32Ir = @'
declare double @vsum(i32, ...)
declare double @vsum2(i32, i32, ...)
define double @call_vsum32() {
  %r = call double (i32, ...) @vsum(i32 1, double 1.5)
  ret double %r
}
define double @call_vsum32b() {
  %r = call double (i32, i32, ...) @vsum2(i32 1, i32 2, double 1.5)
  ret double %r
}
'@

$calleeIr = @'
define double @callee(double %x) {
  %r = fadd double %x, 1.000000e+00
  ret double %r
}
'@

$callerIr = @'
declare double @callee(double)
define double @caller(double %x) {
  %y = fadd double %x, 1.500000e+00
  %r = call double @callee(double %y)
  ret double %r
}
'@

$sretIr = @'
%big = type { i64, i64, i64, i64, i64 }
@g = dso_local global %big zeroinitializer, align 8
declare i64 @byval_read(ptr byval(%big) align 8)
define %big @ret_big(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e) {
  %p0 = insertvalue %big undef, i64 %a, 0
  %p1 = insertvalue %big %p0, i64 %b, 1
  %p2 = insertvalue %big %p1, i64 %c, 2
  %p3 = insertvalue %big %p2, i64 %d, 3
  %p4 = insertvalue %big %p3, i64 %e, 4
  ret %big %p4
}
define i64 @caller_byval() {
  %r = call i64 @byval_read(ptr byval(%big) align 8 @g)
  ret i64 %r
}
define i64 @byval_write(ptr byval(%big) align 8 %p) {
  %f0 = load i64, ptr %p
  store i64 0, ptr %p
  ret i64 %f0
}
'@

$abi = Join-Path $dir 'abi.ll'
$var = Join-Path $dir 'var.ll'
$var32 = Join-Path $dir 'var32.ll'
$callee = Join-Path $dir 'callee.ll'
$caller = Join-Path $dir 'caller.ll'
$sret = Join-Path $dir 'sret.ll'
Write-Ascii $abi $abiIr
Write-Ascii $var $varIr
Write-Ascii $var32 $var32Ir
Write-Ascii $callee $calleeIr
Write-Ascii $caller $callerIr
Write-Ascii $sret $sretIr

switch ($Case) {
    'A' {
        Show-Case 'A. same IR under lp64d / lp64f / lp64'
        foreach ($cfg in @(@('lp64d','+m,+a,+f,+d'), @('lp64f','+m,+a,+f'), @('lp64','+m,+a'))) {
            $name = $cfg[0]; $feat = $cfg[1]
            $out = Join-Path $dir ("abi-" + $name + ".s")
            & $llc -mtriple=riscv64 ("-mattr=" + $feat) ("-target-abi=" + $name) -O2 $abi -o $out
            Write-Host ("---- " + $name + " exit=" + $LASTEXITCODE) -ForegroundColor Yellow
            Show-Func $out 'sum9'
            Show-Func $out 'sumd9'
            Show-Func $out 'sumf9'
            Show-Func $out 'mixed'
        }
    }
    'B' {
        Show-Case 'B. varargs: GPR placement and RV32 alignment'
        $o64 = Join-Path $dir 'var-lp64d.s'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -target-abi=lp64d -O2 $var -o $o64
        Write-Host ("rv64 exit=" + $LASTEXITCODE)
        Show-Func $o64 'call_vsum'
        Show-Func $o64 'vararg'
        $o32 = Join-Path $dir 'var-ilp32.s'
        & $llc -mtriple=riscv32 '-mattr=+m,+a' -target-abi=ilp32 -O2 $var32 -o $o32
        Write-Host ("rv32 exit=" + $LASTEXITCODE)
        Show-Func $o32 'call_vsum32'
        Show-Func $o32 'call_vsum32b'
    }
    'C' {
        Show-Case 'C. -target-abi warnings, e_flags, link error'
        foreach ($t in @(
            @('riscv64','ilp32','+m,+a'),
            @('riscv32','lp64','+m,+a'),
            @('riscv64','lp64q','+m,+a'),
            @('riscv64','foo','+m,+a'),
            @('riscv64','lp64d','+m,+a,+f'))) {
            $tri = $t[0]; $abiName = $t[1]; $feat = $t[2]
            Write-Host ("-- " + $tri + " " + $abiName + " " + $feat)
            $o = & $llc ("-mtriple=" + $tri) ("-mattr=" + $feat) ("-target-abi=" + $abiName) -O2 $abi -o (Join-Path $dir 'probe.s') 2>&1
            Show-Out $o
            Write-Host ("exit=" + $LASTEXITCODE)
        }
        $cobj = Join-Path $dir 'callee-lp64.o'
        $pobj = Join-Path $dir 'caller-lp64d.o'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -target-abi=lp64 -O2 -filetype=obj $callee -o $cobj
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -target-abi=lp64d -O2 -filetype=obj $caller -o $pobj
        Write-Host '-- e_flags'
        Show-Out (& $readelf -h $pobj 2>&1 | Select-String 'Flags')
        Show-Out (& $readelf -h $cobj 2>&1 | Select-String 'Flags')
        Write-Host '-- ld.lld mismatch'
        $lo = & $ld -shared $pobj $cobj -o (Join-Path $dir 'mismatch.so') 2>&1
        Show-Out $lo
        Write-Host ("link exit=" + $LASTEXITCODE)
    }
    'D' {
        Show-Case 'D. hand-written asm bypasses the e_flags check'
        $pobj = Join-Path $dir 'caller-lp64d.o'
        if (-not (Test-Path $pobj)) {
            & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -target-abi=lp64d -O2 -filetype=obj $caller -o $pobj
        }
        $asm = @'
	.attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2"
	.text
	.globl callee
callee:
	fcvt.d.l	fa0, a0
	ret
'@
        $asrc = Join-Path $dir 'callee-wrong.s'
        $aobj = Join-Path $dir 'callee-wrong.o'
        Write-Ascii $asrc $asm
        & $mc -triple=riscv64 '-mattr=+m,+a,+f,+d' -filetype=obj $asrc -o $aobj
        Write-Host ("mc exit=" + $LASTEXITCODE)
        Show-Out (& $readelf -h $aobj 2>&1 | Select-String 'Flags')
        $so = Join-Path $dir 'mismatch-asm.so'
        $lo = & $ld -shared $pobj $aobj -o $so 2>&1
        Show-Out $lo
        Write-Host ("link exit=" + $LASTEXITCODE)
        if (Test-Path $so) {
            Show-Out (& $objdump -d '--mattr=+m,+a,+f,+d' $so 2>&1 | Select-String '<caller>|<callee>|fmv|fadd|fcvt|li\s+a0|slli|jalr')
        }
    }
    'E' {
        Show-Case 'E. sret and byval'
        $out = Join-Path $dir 'sret.s'
        & $llc -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -target-abi=lp64d -O2 $sret -o $out
        Write-Host ("llc exit=" + $LASTEXITCODE)
        Show-Func $out 'ret_big'
        Show-Func $out 'caller_byval'
        Show-Func $out 'byval_write'
    }
    default {
        Write-Host 'Usage: run.ps1 labs\12-calling-convention.ps1 -Case A|B|C|D|E|ALL'
    }
}
exit 0
