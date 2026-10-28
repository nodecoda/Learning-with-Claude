/* Ch 19/24 的完整启动代码：对比 code/05_cortexm 的最小版本，这里补上
 * .data 拷贝与 .bss 清零，并处理 -nostdlib 之后 memcpy/memset 缺失的问题。
 *
 * 环境：arm-none-eabi-gcc 10.3 / qemu-system-arm 6.2 (mps2-an385, cortex-m3)
 * 运行：./build.sh
 */
#include <stdint.h>

/* 链接脚本导出的符号 */
extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;

/* MPS2-AN385 的 UART0 是 CMSDK APB UART，不是 PL011：
 *   0x00 DR    数据
 *   0x04 STATE bit0 = TX buffer full, bit1 = RX buffer full
 *   0x08 CTRL  bit0 = TX enable, bit1 = RX enable
 * 用 PL011 的 FR(0x18) bit5 判断会永远等不到，表现为静默卡死。
 */
#define UART0_BASE 0x40004000u
#define UART_DR    (*(volatile uint32_t *)(UART0_BASE + 0x00))
#define UART_STATE (*(volatile uint32_t *)(UART0_BASE + 0x04))
#define UART_CTRL  (*(volatile uint32_t *)(UART0_BASE + 0x08))

static void putc_(char c) {
  while (UART_STATE & 0x1u) { }   /* 等 TX 空 */
  UART_DR = (uint32_t)c;
}

static void puts_(const char *s) { while (*s) putc_(*s++); }

void reset_handler(void);
void default_handler(void);

__attribute__((section(".vectors"), used))
const void *vectors[16] = {
    (void *)0x20010000,          /* 初始栈指针：RAM 顶端 */
    (void *)reset_handler,       /* Reset */
    (void *)default_handler,     /* NMI */
    (void *)default_handler,     /* HardFault */
    (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
};

void default_handler(void) {
  puts_("FAULT\n");
  for (;;) { }
}

/* 测试用的全局变量 */
int g_init = 1234;    /* 有初值 → .data，启动时必须从 FLASH 拷到 RAM */
int g_zero;           /* 无初值 → .bss，启动时必须清零 */

/* -nostdlib 之后，编译器自动生成的 memcpy/memset 调用没有实现，
 * 链接会报 undefined reference。而且如果自己写的 memset 用了标准签名，
 * GCC 会把它识别成内建函数并改写成对自己的调用 —— 实测会无限递归。
 * 两个坑都要处理：自己实现 + 编译时加 -fno-builtin。
 */
void *memcpy(void *dst, const void *src, unsigned n) {
  unsigned char *d = (unsigned char *)dst;
  const unsigned char *s = (const unsigned char *)src;
  while (n--) *d++ = *s++;
  return dst;
}

void *memset(void *dst, int v, unsigned n) {
  unsigned char *d = (unsigned char *)dst;
  while (n--) *d++ = (unsigned char)v;
  return dst;
}

void reset_handler(void) {
  /* 故意跳过 .data 拷贝 */
  (void)_sidata; (void)_sdata; (void)_edata;
  uint32_t *src = 0; uint32_t *dst;


  /* ② .bss: 清零 */
  dst = &_sbss;
  while (dst < &_ebss) *dst++ = 0;

  /* ③ 外设初始化 */
  UART_CTRL = 0x3u;

  /* ④ 应用 */
  puts_("start\n");
  if (g_init == 1234) puts_("data ok\n"); else puts_("data BAD\n");
  if (g_zero == 0)     puts_("bss ok\n");  else puts_("bss BAD\n");

  for (;;) { }
}
