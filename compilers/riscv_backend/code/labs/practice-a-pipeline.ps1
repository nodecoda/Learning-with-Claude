# Practice A: six artifacts from one .ll. ASCII only.
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$dir = Join-Path $env:TEMP 'riscv-practice-a'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ll = Join-Path $dir 'pa.ll'

$llLines = @(
    'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
    'target triple = "riscv64-unknown-elf"'
    ''
    'declare i64 @g(i64)'
    ''
    'define i64 @kernel(i64 %a, i64 %b, i1 %c) {'
    'entry:'
    '  %m = mul i64 %a, %b'
    '  %s = select i1 %c, i64 %m, i64 7'
    '  %call = call i64 @g(i64 %s)'
    '  %r = add i64 %call, 3'
    '  ret i64 %r'
    '}'
)
[IO.File]::WriteAllText($ll, ($llLines -join "`n"), (New-Object Text.UTF8Encoding($false)))

$asm  = Join-Path $dir 'pa.s'
$isel = Join-Path $dir 'pa-isel.mir'
$ra   = Join-Path $dir 'pa-ra.mir'
$obj  = Join-Path $dir 'pa.o'

& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+relax' -O2 $ll -o $asm
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+relax' -O2 -stop-after=riscv-isel $ll -o $isel
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+relax' -O2 -stop-after=prolog-epilog $ll -o $ra
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m,+relax' -O2 -filetype=obj $ll -o $obj

function Show-MirFunction([string]$Path, [string]$Name, [int]$Count) {
    $lines = Get-Content -LiteralPath $Path
    $start = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^name:\s+$Name\s*$") { $start = $i; break }
    }
    if ($start -lt 0) { Write-Host "MIR function not found: $Name"; return }
    $end = [Math]::Min($start + $Count, $lines.Count - 1)
    $lines[$start..$end]
}

Write-Host '== 1. IR ==' -ForegroundColor Cyan
Get-Content -LiteralPath $ll

Write-Host ''
Write-Host '== 2. MIR after isel ==' -ForegroundColor Cyan
Show-MirFunction $isel 'kernel' 55

Write-Host ''
Write-Host '== 3. MIR after RA + prologue/epilogue ==' -ForegroundColor Cyan
Show-MirFunction $ra 'kernel' 80

Write-Host ''
Write-Host '== 4. final asm ==' -ForegroundColor Cyan
Get-Content -LiteralPath $asm

Write-Host ''
Write-Host '== 5. objdump ==' -ForegroundColor Cyan
& "$bin\llvm-objdump.exe" -d --no-show-raw-insn $obj

Write-Host ''
Write-Host '== 6. relocations ==' -ForegroundColor Cyan
& "$bin\llvm-readobj.exe" --relocations $obj

Write-Host ''
Write-Host "Artifacts: $dir" -ForegroundColor DarkGray