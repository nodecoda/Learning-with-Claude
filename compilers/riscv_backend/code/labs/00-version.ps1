# Ch 00 baseline check: llc / llvm-mc / llvm-tblgen version and target registration
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

Write-Host '== llc --version ==' -ForegroundColor Cyan
& "$bin\llc.exe" --version

Write-Host ''
Write-Host '== riscv targets ==' -ForegroundColor Cyan
& "$bin\llc.exe" --version | Select-String -Pattern 'riscv'

Write-Host ''
Write-Host '== tools ==' -ForegroundColor Cyan
foreach ($t in 'llc', 'llvm-mc', 'llvm-objdump', 'llvm-readobj', 'llvm-tblgen', 'llvm-mca', 'ld.lld') {
    '{0,-14} {1}' -f $t, (Test-Path (Join-Path $bin "$t.exe"))
}

Write-Host ''
Write-Host '== first RISC-V function ==' -ForegroundColor Cyan
$ll = Join-Path $env:TEMP 'riscv-ch00.ll'
$lines = @(
    'target triple = "riscv64-unknown-elf"'
    'define i64 @mul_add(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  %r = add i64 %m, 7'
    '  ret i64 %r'
    '}'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 $ll -o -