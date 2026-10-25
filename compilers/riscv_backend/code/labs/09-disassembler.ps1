# Ch 09 lab: disassembly, RVC, aliases, objdump options.
# ASCII only.
$ErrorActionPreference = 'Continue'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }
$mc = Join-Path $bin 'llvm-mc.exe'
$objdump = Join-Path $bin 'llvm-objdump.exe'

function Show-Case([string]$Title) {
    Write-Host ''
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Show-Captured($Items) {
    foreach ($line in $Items) { Write-Host ("OUT: " + $line.ToString()) }
}

function Invoke-Disasm {
    param([string]$Bytes, [string]$Name, [string[]]$Extra)
    $p = Join-Path $env:TEMP $Name
    [IO.File]::WriteAllText($p, $Bytes, (New-Object Text.UTF8Encoding($false)))
    & $mc -triple=riscv64 @Extra -disassemble $p 2>&1
}

$bytes = @(
  '0x13,0x85,0xa5,0x02','0x13,0xc5,0xf5,0xff','0x22,0x45','0x2a,0xc6',
  '0x02,0x95','0x01,0x00','0x05,0x05','0x7d,0x55','0x88,0x45',
  '0x2e,0x85','0x82,0x80','0x65,0xfd','0x01,0xa8','0xef,0x00,0x10,0x00'
)

Show-Case 'A. default disassembly'
$i = 0
foreach ($s in $bytes) {
    $i++
    Write-Host "-- $s"
    Invoke-Disasm $s ("riscv-ch09-a-{0}.txt" -f $i) @('-mattr=+c')
}

Show-Case 'B. no-aliases disassembly'
$i = 0
foreach ($s in @('0x13,0xc5,0xf5,0xff','0x02,0x95','0x2e,0x85','0x82,0x80','0x65,0xfd','0x7d,0x55')) {
    $i++
    Write-Host "-- $s"
    Invoke-Disasm $s ("riscv-ch09-b-{0}.txt" -f $i) @('-mattr=+c', '-M', 'no-aliases')
}

Show-Case 'C. objdump on an object with RVC'
$asm = @(
  "`t.text",
  "`t.globl`tf",
  "f:",
  "`tlw`ta0, 8(sp)",
  "`tsw`ta0, 12(sp)",
  "`tjalr`tra, 0(a0)",
  "`txori`ta0, a1, -1",
  "`tret"
) -join "`n"
$src = Join-Path $env:TEMP 'riscv-ch09.asm'
$obj = Join-Path $env:TEMP 'riscv-ch09.o'
[IO.File]::WriteAllText($src, $asm, (New-Object Text.UTF8Encoding($false)))
& $mc -triple=riscv64 '-mattr=+c' -filetype=obj $src -o $obj
Write-Host "assemble exit=$LASTEXITCODE"
Write-Host '-- default'
& $objdump -d $obj 2>&1
Write-Host '-- no-aliases'
& $objdump -d -M no-aliases $obj 2>&1
Write-Host '-- numeric'
& $objdump -d -M numeric $obj 2>&1

Show-Case 'D. invalid and truncated streams'
$i = 0
foreach ($s in @('0x00,0x00','0xff,0xff,0xff,0xff','0xef,0x00,0x10')) {
    $i++
    Write-Host "-- $s"
    $o = & cmd /c "echo $s| `"$mc`" -triple=riscv64 -mattr=+c -disassemble" 2>&1
    Show-Captured $o
    Write-Host "exit=$LASTEXITCODE"
}

Show-Case 'E. objdump --mattr pitfalls'
Write-Host '-- mattr=-c on an object whose attributes enable C'
& $objdump -d '--mattr=-c' $obj 2>&1
Write-Host '-- bogus feature'
$o = & $objdump -d '--mattr=+notreal' $obj 2>&1
Show-Captured $o
Write-Host "exit=$LASTEXITCODE"

Show-Case 'F. compressed bytes without +c'
$o = & cmd /c "echo 0x22,0x45| `"$mc`" -triple=riscv64 -disassemble" 2>&1
Show-Captured $o
Write-Host "exit=$LASTEXITCODE"
exit 0