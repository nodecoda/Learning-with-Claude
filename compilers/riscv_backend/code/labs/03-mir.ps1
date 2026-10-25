# Ch 03 MIR probe: pass structure, stop points, handwritten MIR. ASCII only.
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$tmp = Join-Path $env:TEMP 'riscv-ch03-mir'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$ll = Join-Path $tmp 'ch03.ll'
$llLines = @(
    'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
    'target triple = "riscv64-unknown-elf"'
    'define i64 @mul_add(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  %r = add i64 %m, 7'
    '  ret i64 %r'
    '}'
)
[IO.File]::WriteAllText($ll, ($llLines -join "`n"), (New-Object Text.UTF8Encoding($false)))

Write-Host '== pass names ==' -ForegroundColor Cyan
$passLog = Join-Path $tmp 'pass.log'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 -debug-pass=Structure $ll -o NUL 2> $passLog
$ErrorActionPreference = $prev
Select-String -LiteralPath $passLog -Pattern 'RISC-V DAG->DAG|Finalize ISel|Greedy Register|Virtual Register Rewriter|RISC-V Insert VSETVLI|Prologue/Epilogue|RISC-V pseudo instruction expansion|RISC-V Assembly Printer' |
    ForEach-Object { $_.Line.Trim() }

Write-Host ''
Write-Host '== stop after riscv-isel (SSA, vregs) ==' -ForegroundColor Cyan
$isel = Join-Path $tmp 'isel.mir'
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 -stop-after=riscv-isel $ll -o $isel
Get-Content -LiteralPath $isel |
    Select-String -Pattern '^name:\s+mul_add' -Context 0,35

Write-Host ''
Write-Host '== stop before riscv-copyelim (physical regs) ==' -ForegroundColor Cyan
$postra = Join-Path $tmp 'postra.mir'
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 -stop-before=riscv-copyelim $ll -o $postra
Get-Content -LiteralPath $postra |
    Select-String -Pattern '^name:\s+mul_add' -Context 0,8
Get-Content -LiteralPath $postra |
    Select-String -Pattern 'noVRegs|registers:'

Write-Host ''
Write-Host '== handwritten MIR: run one pass ==' -ForegroundColor Cyan
$mir = Join-Path $tmp 'hand.mir'
$mirLines = @(
    '--- |'
    '  target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
    '  target triple = "riscv64-unknown-elf"'
    '  define i64 @f(i64 %a) {'
    '    ret i64 %a'
    '  }'
    '...'
    '---'
    'name:            f'
    'alignment:       4'
    'tracksRegLiveness: true'
    'liveins:'
    '  - { reg: ''$x10'' }'
    'body:             |'
    '  bb.0:'
    '    liveins: $x10'
    ''
    '    $x11 = ADDI $x10, 1'
    '    $x10 = ADDI $x10, 2'
    '    PseudoRET implicit $x10'
    '...'
)
[IO.File]::WriteAllText($mir, ($mirLines -join "`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin\llc.exe" -mtriple=riscv64 -run-pass=dead-mi-elimination $mir -o -