# Ch 02 ISA probe: encodings, feature toggles, XLEN, endianness. ASCII only.
$ErrorActionPreference = 'Stop'
$bin = $env:LLVM_BIN
if (-not $bin) { throw 'Run this script through code/tools/run.ps1' }

$tmp = Join-Path $env:TEMP 'riscv-ch02-isa'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

Write-Host '== encodings: six formats, no +c ==' -ForegroundColor Cyan
$s = Join-Path $tmp 'enc.s'
$lines = @(
    'addi a0, a1, 42'
    'add a0, a1, a2'
    'sub a0, a1, a2'
    'sw a0, 12(sp)'
    'ld a0, -8(s0)'
    'beq a0, a1, 16'
    'lui a0, 0x12345'
    'jal ra, 2048'
)
[IO.File]::WriteAllText($s, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin\llvm-mc.exe" -triple=riscv64 '-mattr=+m,+a,+f,+d' -show-encoding $s

Write-Host ''
Write-Host '== M on/off ==' -ForegroundColor Cyan
$mul = Join-Path $tmp 'mul.ll'
$mulLines = @(
    'target triple = "riscv64-unknown-elf"'
    'define i64 @f(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  ret i64 %m'
    '}'
)
[IO.File]::WriteAllText($mul, ($mulLines -join "`n"), (New-Object Text.UTF8Encoding($false)))
foreach ($m in @('+m', '-m')) {
    "=== mattr=$m ==="
    & "$bin\llc.exe" -mtriple=riscv64 "-mattr=$m" -O2 $mul -o -
}

Write-Host ''
Write-Host '== RV32 i64 multiply ==' -ForegroundColor Cyan
$rv32 = Join-Path $tmp 'rv32.ll'
$rv32Lines = @(
    'target triple = "riscv32-unknown-elf"'
    'define i32 @m32(i32 %a, i32 %b) {'
    '  %m = mul i32 %a, %b'
    '  %r = add i32 %m, 7'
    '  ret i32 %r'
    '}'
    'define i64 @m64(i64 %a, i64 %b) {'
    '  %m = mul i64 %a, %b'
    '  ret i64 %m'
    '}'
)
[IO.File]::WriteAllText($rv32, ($rv32Lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin\llc.exe" -mtriple=riscv32 '-mattr=+m' -O2 $rv32 -o -

Write-Host ''
Write-Host '== endianness: same module, two triples ==' -ForegroundColor Cyan
$end = Join-Path $tmp 'endian.ll'
$endLines = @(
    'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
    'target triple = "riscv64-unknown-elf"'
    '@g = global i32 16909060'
    'define i32 @ld() {'
    '  %v = load i32, ptr @g, align 4'
    '  ret i32 %v'
    '}'
)
[IO.File]::WriteAllText($end, ($endLines -join "`n"), (New-Object Text.UTF8Encoding($false)))
foreach ($triple in @('riscv64', 'riscv64be')) {
    "=== $triple ==="
    $obj = Join-Path $tmp "$triple.o"
    & "$bin\llc.exe" "-mtriple=$triple" '-mattr=+m' -O2 -filetype=obj $end -o $obj
    & "$bin\llvm-readobj.exe" --file-headers --hex-dump=.data $obj |
        Select-String -Pattern 'Format:|DataEncoding|0x00000000'
}

Write-Host ''
Write-Host '== unknown feature: warning only ==' -ForegroundColor Cyan
$badLog = Join-Path $tmp 'bad.log'
$prev = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$badProc = Start-Process -FilePath "$bin\llc.exe" -ArgumentList @('-mtriple=riscv64','-mattr=+notafeature','-O2',$mul,'-o',(Join-Path $tmp 'bad.s')) -NoNewWindow -Wait -PassThru -RedirectStandardError $badLog
$badCode = $badProc.ExitCode
$ErrorActionPreference = $prev
"EXIT=$badCode"
Get-Content -LiteralPath $badLog | Select-Object -First 2