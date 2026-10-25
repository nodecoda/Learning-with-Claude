# practice-c-abi-audit.ps1 -- six ABI signatures and a byval IR case (ASCII only)
$ErrorActionPreference = 'Stop'
$llvm = $env:LLVM_BIN
$clang = Join-Path (Split-Path -Parent $llvm) 'bin\clang.exe'
if (-not $llvm) { throw 'LLVM_BIN is not set; run through code/tools/run.ps1' }
if (-not (Test-Path $clang)) { throw "clang not found: $clang" }
$work = Join-Path $env:TEMP 'riscv-course-practiceC'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Set-Location $work

$c = @(
  'struct Small { long a; long b; };',
  'struct Big { long a; long b; long c; long d; };',
  '',
  'long ints8(long a, long b, long c, long d, long e, long f, long g, long h) {',
  '  return a + b + c + d + e + f + g + h;',
  '}',
  '',
  'long ints9(long a, long b, long c, long d, long e, long f, long g, long h, long i) {',
  '  return a + b + c + d + e + f + g + h + i;',
  '}',
  '',
  'long take_small(struct Small s) { return s.a + s.b; }',
  'long take_big(struct Big s) { return s.a + s.b + s.c + s.d; }',
  '',
  'struct Big make_big(long x) {',
  '  struct Big r = { x, x + 1, x + 2, x + 3 };',
  '  return r;',
  '}',
  '',
  'double mixfp(float a, float b, double c, double d, float e) {',
  '  return a + b + c + d + e;',
  '}',
  '',
  'long sumv(int n, ...) {',
  '  __builtin_va_list ap;',
  '  __builtin_va_start(ap, n);',
  '  long s = 0;',
  '  for (int i = 0; i < n; i++)',
  '    s += __builtin_va_arg(ap, long);',
  '  __builtin_va_end(ap);',
  '  return s;',
  '}',
  '',
  'long call_ints9(long a, long b, long c, long d, long e, long f, long g, long h, long i) {',
  '  return ints9(a, b, c, d, e, f, g, h, i);',
  '}',
  '',
  'long call_take_big(long x) {',
  '  struct Big b = { x, x + 1, x + 2, x + 3 };',
  '  return take_big(b);',
  '}',
  '',
  'long use_make(long x) {',
  '  struct Big b = make_big(x);',
  '  return b.a + b.d;',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'abi.c'), $c, (New-Object Text.UTF8Encoding($false)))

& $clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O0 -S abi.c -o abi-O0.s
& $clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O0 -S -emit-llvm abi.c -o abi-O0.ll

Write-Host '=== ints8: a0-a7 ==='
Get-Content abi-O0.s | Select-String '^ints8:' -Context 0,10
Write-Host '=== ints9: ninth argument on stack ==='
Get-Content abi-O0.s | Select-String '^ints9:' -Context 0,18
Write-Host '=== take_big: indirect pointer ==='
Get-Content abi-O0.s | Select-String '^take_big:' -Context 0,22
Write-Host '=== call_take_big: caller-owned copy + pointer ==='
Get-Content abi-O0.s | Select-String '^call_take_big:' -Context 0,32
Write-Host '=== take_small: two integer chunks ==='
Get-Content abi-O0.s | Select-String '^take_small:' -Context 0,12
Write-Host '=== make_big: sret ==='
Get-Content abi-O0.s | Select-String '^make_big:' -Context 0,16
Write-Host '=== use_make: sret slot ==='
Get-Content abi-O0.s | Select-String '^use_make:' -Context 0,24
Write-Host '=== mixfp: fa0-fa4 ==='
Get-Content abi-O0.s | Select-String '^mixfp:' -Context 0,18
Write-Host '=== sumv: varargs save area ==='
Get-Content abi-O0.s | Select-String '^sumv:' -Context 0,30
Write-Host '=== call_ints9: outgoing stack argument ==='
Get-Content abi-O0.s | Select-String '^call_ints9:' -Context 0,30
Write-Host '=== IR attributes ==='
Get-Content abi-O0.ll | Select-String 'define.*(take_big|take_small|make_big|sumv)' | ForEach-Object { $_.Line.Trim() }

$byval = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"',
  'target triple = "riscv64-unknown-elf"',
  '%Big = type { i64, i64, i64, i64 }',
  'define i64 @byval_big(ptr byval(%Big) align 8 %p) {',
  '  %a = load i64, ptr %p',
  '  %q = getelementptr i64, ptr %p, i64 3',
  '  %d = load i64, ptr %q',
  '  %r = add i64 %a, %d',
  '  ret i64 %r',
  '}',
  'define i64 @call_byval(i64 %x) {',
  '  %s = alloca %Big, align 8',
  '  %p0 = getelementptr %Big, ptr %s, i64 0, i32 0',
  '  store i64 %x, ptr %p0',
  '  %p1 = getelementptr %Big, ptr %s, i64 0, i32 1',
  '  store i64 1, ptr %p1',
  '  %p2 = getelementptr %Big, ptr %s, i64 0, i32 2',
  '  store i64 2, ptr %p2',
  '  %p3 = getelementptr %Big, ptr %s, i64 0, i32 3',
  '  store i64 3, ptr %p3',
  '  %r = call i64 @byval_big(ptr byval(%Big) align 8 %s)',
  '  ret i64 %r',
  '}'
)
[IO.File]::WriteAllLines((Join-Path $work 'byval.ll'), $byval, (New-Object Text.UTF8Encoding($false)))
& (Join-Path $llvm 'llc.exe') -mtriple=riscv64 -O0 byval.ll -o byval-O0.s
Write-Host '=== hand-written byval at -O0 ==='
Get-Content byval-O0.s | Select-String '^byval_big:' -Context 0,10
Get-Content byval-O0.s | Select-String '^call_byval:' -Context 0,30