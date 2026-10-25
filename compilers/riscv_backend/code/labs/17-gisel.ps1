# Chapter 17 lab: GlobalISel on RISC-V (SelectionDAG vs GlobalISel)
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$work = Join-Path $env:TEMP 'rvlab17'
New-Item -ItemType Directory -Force $work | Out-Null
Set-Location $work
$enc = New-Object Text.UTF8Encoding($false)

$ir = @(
    'target triple = "riscv64-unknown-elf"'
    ''
    'define i64 @mul_add(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  %r = add i64 %m, 7'
    '  ret i64 %r'
    '}'
    ''
    'define i32 @sel(i1 %c, i32 %a, i32 %b) {'
    '  %s = select i1 %c, i32 %a, i32 %b'
    '  ret i32 %s'
    '}'
    ''
    'define i64 @ldst(ptr %p, i64 %v) {'
    '  store i64 %v, ptr %p, align 8'
    '  %x = load i64, ptr %p, align 8'
    '  ret i64 %x'
    '}'
) -join "`n"
[IO.File]::WriteAllText("$work\basic.ll", $ir, $enc)

Write-Host '== SelectionDAG ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 basic.ll -o sdag.s
Get-Content sdag.s

Write-Host ''
Write-Host '== GlobalISel ==' -ForegroundColor Cyan
& "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 -global-isel basic.ll -o gisel.s
Get-Content gisel.s

Write-Host ''
Write-Host '== Compare (=> only in GlobalISel, <= only in SelectionDAG) ==' -ForegroundColor Cyan
Compare-Object (Get-Content sdag.s) (Get-Content gisel.s) | Format-Table -AutoSize

Write-Host ''
Write-Host '== Four stages ==' -ForegroundColor Cyan
foreach ($stage in 'irtranslator', 'legalizer', 'regbankselect', 'instruction-select') {
    Write-Host ('--- ' + $stage + ' ---')
    & "$bin\llc.exe" -mtriple=riscv64 -mattr=+m -O2 -global-isel "-stop-after=$stage" basic.ll -o - |
        Select-String -Pattern 'legalized:|regBankSelected:|selected:|%[0-9]+:' |
        Select-Object -First 10
}

Write-Host ''
Write-Host '== Fallback: vector reduce is not legalized by GISel ==' -ForegroundColor Cyan
$gap = @(
    'target triple = "riscv64-unknown-elf"'
    'define i64 @vsum(ptr %p) {'
    '  %v = load <4 x i32>, ptr %p, align 16'
    '  %r = call i32 @llvm.vector.reduce.add.v4i32(<4 x i32> %v)'
    '  %z = zext i32 %r to i64'
    '  ret i64 %z'
    '}'
    'declare i32 @llvm.vector.reduce.add.v4i32(<4 x i32>)'
) -join "`n"
[IO.File]::WriteAllText("$work\gap.ll", $gap, $enc)

$ErrorActionPreference = 'Continue'
foreach ($mode in 0, 1, 2) {
    Write-Host ('--- -global-isel-abort=' + $mode + ' ---')
    $out = & "$bin\llc.exe" -mtriple=riscv64 "-mattr=+m,+v" -O2 -global-isel "-global-isel-abort=$mode" gap.ll -o ("gap" + $mode + ".s") 2>&1
    Write-Host ('exit=' + $LASTEXITCODE)
    $out | Select-String -Pattern 'LLVM ERROR|warning' | Select-Object -First 3
}