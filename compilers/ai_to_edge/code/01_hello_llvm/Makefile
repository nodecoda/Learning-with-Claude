# 环境：Ubuntu 22.04 / LLVM 14 / x86_64
# 运行：make
LLVM_BIN ?= /usr/lib/llvm-14/bin
CC := $(LLVM_BIN)/clang
LLC := $(LLVM_BIN)/llc

all: add_a64.s add_a64.o

add.ll: add.c
	$(CC) --target=aarch64-linux-gnu -O2 -S -emit-llvm $< -o $@

add_a64.s: add.ll
	$(LLC) -march=aarch64 $< -o $@

add_a64.o: add.ll
	$(LLC) -march=aarch64 -filetype=obj $< -o $@

clean:
	rm -f add.ll add_a64.s add_a64.o

.PHONY: all clean
