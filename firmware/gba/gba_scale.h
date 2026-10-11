/* ==================================================================
 * gba_scale.h — 纯 C 的 240x160 -> 显示缓冲缩放（无 MicroPython 依赖）
 *
 * 单独成文件的目的：让这段**新写的** C 代码能在宿主上被真正执行并由
 * ASan/UBSan 检查（见 tools/hosttest/host_scale_test.c）。
 * 只靠交叉编译通过，不能证明下标、边界与像素落点是对的。
 *
 * 缓冲约定：显示缓冲恒为 720x480、**行距 720 像素**（uint16 RGB565）。
 * 千万不要试图改变行距：CanMV 的 Show_image 从 image->w 取步长
 * （modmedia.display.c: buffer->vf_info.v_frame.stride[0] = image->w * bpp），
 * 步长不符会让画面斜切，而且 py_image_alloc 只检查 size > buflen，
 * 属于静默出错。
 * ================================================================== */
#ifndef GBA_SCALE_H
#define GBA_SCALE_H

#include <stddef.h>
#include <stdint.h>

#define GBA_FB_W 240
#define GBA_FB_H 160
#define GBA_DISP_W 720
#define GBA_DISP_H 480

/* 缩放并把画面放到缓冲内的 (off_x, off_y)。
 * 名字带 _pixels 以免与 gba_module.c 里的 MicroPython 包装函数重名。
 * dst 必须先被调用者清空（本函数只写游戏画面覆盖到的像素）。
 *
 * 返回 0 成功；负数表示参数非法（越界/倍率不支持）。 */
int gba_scale_into_pixels(uint16_t* dst, const uint16_t* src,
                          int scale, int off_x, int off_y);

/* 从 (off_x, off_y) 开始，用零填充整个缓冲。 */
void gba_clear_disp(uint16_t* dst);

#endif /* GBA_SCALE_H */
