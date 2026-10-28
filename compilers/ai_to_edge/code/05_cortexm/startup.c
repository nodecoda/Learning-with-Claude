/* Cortex-M 最小启动 + 半主机输出。
 * 环境：arm-none-eabi-gcc 10.3 / qemu-system-arm -M mps2-an385 (Cortex-M3)
 * 运行：./build.sh
 */
#include <stdint.h>

/* MPS2-AN385 的 UART0：CMSDK APB UART，基址 0x40004000
 * 注意：它不是 PL011。寄存器布局完全不同：
 *   0x00 DR    数据
 *   0x04 STATE bit0=TX full, bit1=RX full
 *   0x08 CTRL  bit0=TX enable, bit1=RX enable
 * 用 PL011 的 FR(0x18)/bit5 判断会永远不等，表现为「程序卡死不输出」。 */
#define UART0_BASE 0x40004000u
#define UART_DR    (*(volatile uint32_t *)(UART0_BASE + 0x00))
#define UART_STATE (*(volatile uint32_t *)(UART0_BASE + 0x04))
#define UART_CTRL  (*(volatile uint32_t *)(UART0_BASE + 0x08))

static void uart_init(void) {
  UART_CTRL = 0x3u;   /* TX enable | RX enable */
}

static void putc_(char c) {
  /* STATE bit0 = TX buffer full */
  while (UART_STATE & 0x1u) { }
  UART_DR = (uint32_t)c;
}

static void puts_(const char *s) {
  while (*s) putc_(*s++);
}

static void putint_(int32_t v) {
  char buf[12];
  int i = 0;
  if (v < 0) { putc_('-'); v = -v; }
  if (v == 0) { putc_('0'); return; }
  while (v > 0) { buf[i++] = (char)('0' + v % 10); v /= 10; }
  while (i--) putc_(buf[i]);
}

extern int32_t compute(int32_t a, int32_t b);

void reset_handler(void);
void default_handler(void);

__attribute__((section(".vectors"), used))
const void *vectors[16] = {
    (void *)0x20010000,   /* initial stack pointer */
    (void *)reset_handler,
    (void *)default_handler, (void *)default_handler,
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

void reset_handler(void) {
  uart_init();
  puts_("cortex-m3 alive\n");
  putint_(compute(6, 7));
  putc_('\n');
  for (;;) { }
}
