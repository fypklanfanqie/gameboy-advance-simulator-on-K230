/* ==================================================================
 * gba_scale.c — 缩放实现（纯 C，无 MicroPython 依赖）
 *
 * 最近邻整数倍缩放，写入固定行距 720 的 RGB565 缓冲。
 * 单独成文件以便宿主测试能在 ASan/UBSan 下真正执行它。
 * ================================================================== */
#include "gba_scale.h"

#include <string.h>

void gba_clear_disp(uint16_t* dst) {
    if (!dst) {
        return;
    }
    memset(dst, 0, (size_t)GBA_DISP_W * GBA_DISP_H * sizeof(uint16_t));
}

int gba_scale_into_pixels(uint16_t* dst, const uint16_t* src,
                          int scale, int off_x, int off_y) {
    if (!dst || !src) {
        return -1;
    }
    /* 240*3 = 720 恰好铺满宽度，所以只支持 1/2/3 倍 */
    if (scale < 1 || scale > 3) {
        return -2;
    }
    const int out_w = GBA_FB_W * scale;
    const int out_h = GBA_FB_H * scale;
    if (off_x < 0 || off_y < 0 ||
        off_x + out_w > GBA_DISP_W || off_y + out_h > GBA_DISP_H) {
        return -3;
    }

    /* 只在"画面没有铺满整块缓冲"时才清空：
     * 3 倍 + (0,0) 时游戏写满全部 720x480 像素，清空是纯浪费
     * （691 KB memset/帧）；2 倍等布局有留白，留白必须清零否则有残影。 */
    const int covers_all = (off_x == 0 && off_y == 0 && scale == 3);
    if (!covers_all) {
        memset(dst, 0, (size_t)GBA_DISP_W * GBA_DISP_H * sizeof(uint16_t));
    }

    const size_t out_row_bytes = (size_t)out_w * sizeof(uint16_t);
    for (int sy = 0; sy < GBA_FB_H; sy++) {
        const uint16_t* src_row = src + (size_t)sy * GBA_FB_W;
        /* Expand one row horizontally once.  Vertical repeats are exact
           copies, so memcpy avoids repeating the per-pixel inner loop. */
        uint16_t* first_row = dst +
            (size_t)(off_y + sy * scale) * GBA_DISP_W + off_x;
        for (int x = 0; x < GBA_FB_W; x++) {
            const uint16_t v = src_row[x];
            uint16_t* d = first_row + x * scale;
            for (int c = 0; c < scale; c++) {
                d[c] = v;
            }
        }
        for (int rep = 1; rep < scale; rep++) {
            /* Target row stride remains GBA_DISP_W pixels. */
            uint16_t* dst_row = dst +
                (size_t)(off_y + sy * scale + rep) * GBA_DISP_W + off_x;
            memcpy(dst_row, first_row, out_row_bytes);
        }
    }
    return 0;
}
