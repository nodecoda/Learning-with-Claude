/* Ch 22 的 arena 对照实验
 * 环境：gcc 11.4 / Ubuntu 22.04 / x86_64
 * 运行：gcc -O2 -Wall -Wextra arena_bench.c -o arena_bench
 *       ./arena_bench time_malloc_raw
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

#define ITERS 500000
#define SMALL 4096

static volatile unsigned long sink;

typedef struct { char *buf; size_t cap; size_t used; } Arena;

static void arena_init(Arena *a, size_t cap) {
  a->buf = (char *)malloc(cap);
  a->cap = cap; a->used = 0;
}
static void arena_reset(Arena *a) { a->used = 0; }
static void *arena_alloc(Arena *a, size_t n, size_t align) {
  size_t off = (a->used + align - 1) & ~(align - 1);
  if (off + n > a->cap) return NULL;
  a->used = off + n;
  return a->buf + off;
}

static double now_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

/* 关键：用 sink 吃掉指针值，否则 GCC 会把整对 malloc/free 消掉（实测过：
 * 不加这一行，1.5M 次分配只花 0.154 ms，等于没分配）。 */
static double run_malloc_raw(void) {
  double t0 = now_ms();
  for (int i = 0; i < ITERS; i++) {
    void *p[3];
    for (int j = 0; j < 3; j++) { p[j] = malloc(SMALL); sink += (uintptr_t)p[j]; }
    for (int j = 0; j < 3; j++) free(p[j]);
  }
  return now_ms() - t0;
}

static double run_arena_raw(void) {
  Arena a; arena_init(&a, 1u << 20);
  double t0 = now_ms();
  for (int i = 0; i < ITERS; i++) {
    arena_reset(&a);
    for (int j = 0; j < 3; j++) { void *p = arena_alloc(&a, SMALL, 16); sink += (uintptr_t)p; }
  }
  return now_ms() - t0;
}

static double run_malloc_memset(void) {
  double t0 = now_ms();
  for (int i = 0; i < ITERS; i++) {
    void *p[3];
    for (int j = 0; j < 3; j++) { p[j] = malloc(SMALL); memset(p[j], i + j, SMALL); }
    for (int j = 0; j < 3; j++) { sink += *(unsigned char *)p[j]; free(p[j]); }
  }
  return now_ms() - t0;
}

static double run_arena_memset(void) {
  Arena a; arena_init(&a, 1u << 20);
  double t0 = now_ms();
  for (int i = 0; i < ITERS; i++) {
    arena_reset(&a);
    void *p[3];
    for (int j = 0; j < 3; j++) { p[j] = arena_alloc(&a, SMALL, 16); memset(p[j], i + j, SMALL); }
    for (int j = 0; j < 3; j++) sink += *(unsigned char *)p[j];
  }
  return now_ms() - t0;
}

int main(int argc, char **argv) {
  if (argc < 2) { fprintf(stderr, "usage: %s <mode>\n", argv[0]); return 2; }
  double (*fn)(void) = NULL;
  const char *name = NULL;
  if (!strcmp(argv[1], "time_malloc_raw"))   { fn = run_malloc_raw;    name = "malloc_raw"; }
  else if (!strcmp(argv[1], "time_arena_raw"))    { fn = run_arena_raw;     name = "arena_raw"; }
  else if (!strcmp(argv[1], "time_malloc_memset")){ fn = run_malloc_memset; name = "malloc_memset"; }
  else if (!strcmp(argv[1], "time_arena_memset")) { fn = run_arena_memset;  name = "arena_memset"; }
  else if (!strcmp(argv[1], "all")) {
    for (int r = 0; r < 3; r++) {
      printf("round %d: malloc_raw=%.3f arena_raw=%.3f malloc_memset=%.3f arena_memset=%.3f\n",
             r, run_malloc_raw(), run_arena_raw(), run_malloc_memset(), run_arena_memset());
    }
    return 0;
  }
  else { fprintf(stderr, "unknown mode\n"); return 2; }
  double ms = fn();
  printf("%-14s total=%.3f ms  per_alloc=%.1f ns  (%.0f allocations)\n",
         name, ms, ms * 1e6 / (ITERS * 3.0), ITERS * 3.0);
  return 0;
}
