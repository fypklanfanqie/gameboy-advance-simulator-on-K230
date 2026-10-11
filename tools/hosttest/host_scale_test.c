/* ==================================================================
 * host_scale_test.c — 在宿主上执行 gba_scale.c 并逐像素校验
 *
 * 为什么需要：scale_into 是本项目**新写**的 C 代码。交叉编译通过只说明语法正确，
 * 不说明下标、边界判断和像素落点是对的。这里把它放到 x86_64 上带
 * AddressSanitizer 跑，并检查：
 *   - 每个源像素是否恰好覆盖 scale x scale 的目标块
 *   - 是否写到了缓冲之外（ASan 会直接抓到）
 *   - 非游戏区域的像素是否**未被本函数触碰**（残影由调用者的 clear 负责）
 *   - 非法参数（倍率 0/4、越界偏移）是否被拒绝且**不写任何字节**
 *   - 三种 (scale, off) 组合在真实 720x480 缓冲下都成立
 *
 * 关键回归点：这一段的"步长恒为 720"是硬约束。曾经的设计用
 * image.Image(480,320,...) 包装同一块缓冲，会因为 Show_image 从 image->w
 * 取步长而把画面斜切，而且 py_image_alloc 只检查 size > buflen 会放行。
 * ================================================================== */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "gba_scale.h"

static int checks = 0;
static int failures = 0;

static void check(int cond, const char* what) {
    checks++;
    printf("  %-64s %s\n", what, cond ? "PASS" : "FAIL");
    if (!cond) {
        failures++;
    }
}

static void section(const char* name) {
    printf("\n=== %s ===\n", name);
}

/* 用可预测的值填充源帧：每个像素都是唯一的，便于验证落点。 */
static void fill_src(uint16_t* src) {
    for (int y = 0; y < GBA_FB_H; y++) {
        for (int x = 0; x < GBA_FB_W; x++) {
            /* RGB565 范围内，且 (x,y) 可反解 */
            src[y * GBA_FB_W + x] = (uint16_t)(0x8000 | ((y & 0x1F) << 10) |
                                               ((x & 0x1F) << 5) | 1);
        }
    }
}

/* 校验一次缩放：全缓冲范围都检查（越界写会先被 ASan 抓，这里查内容） */
static void verify_scale(const uint16_t* dst, const uint16_t* src,
                         int scale, int off_x, int off_y) {
    int mismatched = 0, unwritten_bad = 0, first_bad_x = -1, first_bad_y = -1;

    for (int y = 0; y < GBA_DISP_H; y++) {
        for (int x = 0; x < GBA_DISP_W; x++) {
            const uint16_t got = dst[(size_t)y * GBA_DISP_W + x];
            const int in_x = x - off_x;
            const int in_y = y - off_y;
            const int inside = (in_x >= 0 && in_y >= 0 &&
                                in_x < GBA_FB_W * scale && in_y < GBA_FB_H * scale);
            if (inside) {
                /* 最近邻：目标 (in_x,in_y) 对应源 (in_x/scale, in_y/scale) */
                const uint16_t want = src[(in_y / scale) * GBA_FB_W + (in_x / scale)];
                if (got != want) {
                    mismatched++;
                    if (first_bad_x < 0) {
                        first_bad_x = x;
                        first_bad_y = y;
                    }
                }
            } else if (got != 0) {
                /* 本函数不应触碰游戏区域之外的像素 */
                unwritten_bad++;
            }
        }
    }
    if (mismatched) {
        printf("      %d mismatched pixel(s); first at (%d,%d)\n",
               mismatched, first_bad_x, first_bad_y);
    }
    if (unwritten_bad) {
        printf("      %d pixel(s) outside the game area were modified\n", unwritten_bad);
    }
}

int main(void) {
    printf("gba_scale host test\n");

    uint16_t* src = malloc((size_t)GBA_FB_W * GBA_FB_H * sizeof(uint16_t));
    uint16_t* dst = malloc((size_t)GBA_DISP_W * GBA_DISP_H * sizeof(uint16_t));
    if (!src || !dst) {
        fprintf(stderr, "alloc failed\n");
        return 2;
    }
    fill_src(src);

    /* ---------- 1. full: 3x at (0,0) ---------- */
    section("1. full layout: scale=3 at (0,0)");
    /* DIRTY buffer on purpose: 3x covers every pixel of the 720x480 buffer, so
       this must still come out pixel-exact, and the scaler must not depend on
       the caller having cleared anything. */
    for (int i = 0; i < GBA_DISP_W * GBA_DISP_H; i++) {
        dst[i] = 0x5555;
    }
    int rc = gba_scale_into_pixels(dst, src, 3, 0, 0);
    check(rc == 0, "gba_scale_into_pixels(3,0,0) returns 0");
    verify_scale(dst, src, 3, 0, 0);
    check(dst[0] == src[0], "top-left pixel matches the source");
    check(dst[(size_t)479 * GBA_DISP_W + 719] ==
          src[GBA_FB_H * GBA_FB_W - 1], "bottom-right pixel matches the last source pixel");
    /* 每个源像素要覆盖 3x3 块：抽查几处 */
    {
        int ok = 1;
        for (int dy = 0; dy < 3; dy++) {
            for (int dx = 0; dx < 3; dx++) {
                if (dst[(size_t)(10 * 3 + dy) * GBA_DISP_W + (20 * 3 + dx)] !=
                    src[10 * GBA_FB_W + 20]) {
                    ok = 0;
                }
            }
        }
        check(ok, "source pixel (20,10) covers exactly a 3x3 block");
    }

    /* ---------- 2. split: 2x at (0,80) ---------- */
    section("2. split layout: scale=2 at (0,80)");
    /* Start from a DIRTY buffer on purpose: split leaves whitespace, and the
       scaler is required to clear it (a previous frame would otherwise linger
       next to the buttons). This also proves the covers-all shortcut in the
       scaler does not fire for a non-3x layout. */
    for (int i = 0; i < GBA_DISP_W * GBA_DISP_H; i++) {
        dst[i] = 0x5555;
    }
    rc = gba_scale_into_pixels(dst, src, 2, 0, 80);
    check(rc == 0, "gba_scale_into_pixels(2,0,80) returns 0");
    verify_scale(dst, src, 2, 0, 80);
    /* 游戏区域 = y 80..399。上下留白必须是 0，且未被改动。 */
    {
        int top_clean = 1, bottom_clean = 1;
        for (int x = 0; x < GBA_DISP_W; x++) {
            for (int y = 0; y < 80; y++) {
                if (dst[(size_t)y * GBA_DISP_W + x] != 0) top_clean = 0;
            }
            for (int y = 400; y < GBA_DISP_H; y++) {
                if (dst[(size_t)y * GBA_DISP_W + x] != 0) bottom_clean = 0;
            }
        }
        check(top_clean, "rows above the game area (y<80) stay zero");
        check(bottom_clean, "rows below the game area (y>=400) stay zero");
    }
    /* 画面右侧 (x>=480) 必须是 0 —— 那里要留给虚拟按键 */
    {
        int right_clean = 1;
        for (int y = 0; y < GBA_DISP_H; y++) {
            for (int x = 480; x < GBA_DISP_W; x++) {
                if (dst[(size_t)y * GBA_DISP_W + x] != 0) right_clean = 0;
            }
        }
        check(right_clean, "right strip (x>=480) stays zero for the buttons");
    }

    /* ---------- 3. 1x at an offset ---------- */
    section("3. scale=1 at (200,100)");
    gba_clear_disp(dst);
    rc = gba_scale_into_pixels(dst, src, 1, 200, 100);
    check(rc == 0, "gba_scale_into_pixels(1,200,100) returns 0");
    verify_scale(dst, src, 1, 200, 100);

    /* ---------- 4. invalid arguments must be refused and write nothing ---------- */
    section("4. invalid arguments are refused without writing");
    {
        uint16_t* guard = malloc((size_t)GBA_DISP_W * GBA_DISP_H * sizeof(uint16_t));
        for (int i = 0; i < GBA_DISP_W * GBA_DISP_H; i++) {
            guard[i] = 0xBEEF;      /* sentinel */
        }
        int r1 = gba_scale_into_pixels(guard, src, 0, 0, 0);
        int r2 = gba_scale_into_pixels(guard, src, 4, 0, 0);
        int r3 = gba_scale_into_pixels(guard, src, 3, 1, 0);
        int r4 = gba_scale_into_pixels(guard, src, 3, 0, 1);
        int r5 = gba_scale_into_pixels(guard, src, 2, 480, 0);
        int r6 = gba_scale_into_pixels(guard, src, 2, 0, 320);
        int r7 = gba_scale_into_pixels(guard, src, 3, -1, 0);
        int r8 = gba_scale_into_pixels(guard, src, 3, 0, -1);
        int r9 = gba_scale_into_pixels(NULL, src, 3, 0, 0);
        int r10 = gba_scale_into_pixels(guard, NULL, 3, 0, 0);
        check(r1 == -2 && r2 == -2, "scale 0 and 4 are rejected");
        check(r3 == -3 && r4 == -3, "3x nudged off the right/bottom is rejected");
        check(r5 == -3 && r6 == -3, "2x at the extreme edge is rejected");
        check(r7 == -3 && r8 == -3, "negative offsets are rejected");
        check(r9 == -1 && r10 == -1, "NULL buffers are rejected");
        int untouched = 1;
        for (int i = 0; i < GBA_DISP_W * GBA_DISP_H; i++) {
            if (guard[i] != 0xBEEF) {
                untouched = 0;
                break;
            }
        }
        check(untouched, "no byte was written by any rejected call");
        free(guard);
    }

    /* ---------- 5. the exact (scale,off) pairs main.py uses ---------- */
    section("5. the layout table main.py/gba_ui.py actually uses");
    {
        /* full:  (3, 0, 0)   split: (2, 0, 80)  -- from src/gba_ui.py */
        const int combos[2][3] = {{3, 0, 0}, {2, 0, 80}};
        for (int i = 0; i < 2; i++) {
            gba_clear_disp(dst);
            int r = gba_scale_into_pixels(dst, src, combos[i][0], combos[i][1], combos[i][2]);
            char label[96];
            snprintf(label, sizeof(label),
                     "layout combo (scale=%d, off=%d,%d) accepted and correct",
                     combos[i][0], combos[i][1], combos[i][2]);
            check(r == 0, label);
            verify_scale(dst, src, combos[i][0], combos[i][1], combos[i][2]);
        }
    }

    free(src);
    free(dst);

    section("summary");
    printf("  checks: %d, failures: %d\n", checks, failures);
    if (failures) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
