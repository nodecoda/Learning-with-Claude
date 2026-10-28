#include <stdio.h>
int ext_var = 100;
int ext_fn(int v) { return v * 3; }
int compute(int x, int n);
int main(void) { printf("compute(4,3) = %d\n", compute(4, 3)); return 0; }
