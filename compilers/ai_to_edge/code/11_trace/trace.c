/* 练习 C 的样本：一条指令的完整来历
 * 环境：LLVM 14 / aarch64-linux-gnu-gcc 11.4
 * 运行：./build.sh
 */
extern int ext_var;              /* 来自另一个目标文件 */
extern int ext_fn(int);          /* 来自另一个目标文件 */

int gvar = 7;                    /* 本文件的全局变量 */

/* 参数用运行时值，避免被常量折叠成常数 */
int compute(int x, int n) {
  int s = 0;
  for (int i = 0; i < n; i++) s += x * (i + 1);
  return s + gvar + ext_var + ext_fn(x);
}
