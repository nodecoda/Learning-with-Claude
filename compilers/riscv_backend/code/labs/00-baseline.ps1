# Ch 00 baseline probe. ASCII only (PowerShell 5.1 reads BOM-less files as ANSI).
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$tmp = Join-Path $env:TEMP 'riscv-ch00-baseline'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

Write-Host '== version ==' -ForegroundColor Cyan
& "$bin\llc.exe" --version | Select-Object -First 6

Write-Host ''
Write-Host '== riscv targets ==' -ForegroundColor Cyan
& "$bin\llc.exe" --version | Select-String -Pattern 'riscv'

Write-Host ''
Write-Host '== tools ==' -ForegroundColor Cyan
foreach ($t in 'llc', 'llvm-mc', 'llvm-objdump', 'llvm-readobj', 'llvm-tblgen', 'llvm-mca', 'ld.lld') {
    '{0,-14} {1}' -f $t, (Test-Path (Join-Path $bin "$t.exe"))
}

$ll = Join-Path $tmp 'first.ll'
$lines = @(
    'target triple = "riscv64-unknown-elf"'
    'define i64 @mul_add(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  %r = add i64 %m, 7'
    '  ret i64 %r'
    '}'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))

Write-Host ''
Write-Host '== first function ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 $ll -o -

Write-Host ''
Write-Host '== .attribute 5 across -mattr ==' -ForegroundColor Cyan
foreach ($m in @('', '+m', '+m,+a,+f,+d', '+m,+a,+f,+d,+c')) {
    "=== mattr='$m' ==="
    if ($m) {
        & "$bin\llc.exe" -mtriple=riscv64 "-mattr=$m" -O2 $ll -o - |
            Select-String -Pattern '\.attribute\s+5'
    } else {
        & "$bin\llc.exe" -mtriple=riscv64 -O2 $ll -o - |
            Select-String -Pattern '\.attribute\s+5'
    }
}

Write-Host ''
Write-Host '== bad feature: warning only ==' -ForegroundColor Cyan
$badLog = Join-Path $tmp 'bad.log'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& "$bin\llc.exe" -mtriple=riscv64 '-mattr=+notafeature' -O2 $ll -o (Join-Path $tmp 'bad.s') 2> $badLog
$badCode = $LASTEXITCODE
$ErrorActionPreference = $prev
"EXIT=$badCode"
Get-Content -LiteralPath $badLog | Select-Object -First 4

Write-Host ''
Write-Host '== disk ==' -ForegroundColor Cyan
Get-PSDrive D | ForEach-Object { 'FreeBytes={0} FreeGiB={1:N2}' -f $_.Free, ($_.Free / 1GB) }

Write-Host ''
Write-Host '== source versions ==' -ForegroundColor Cyan
foreach ($tree in @('D:\Learning-with-Claude\.scratch\llvm-23.1.4-riscv-td', 'D:\llvm-project')) {
    if (Test-Path $tree) {
        $d = (& git -C $tree describe --tags --always 2>&1) -join ' '
        '{0} -> {1}' -f $tree, $d
    }
}