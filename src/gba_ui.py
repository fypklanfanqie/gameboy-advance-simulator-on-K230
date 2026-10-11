#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==================================================================
# 屏幕布局与虚拟按键区域（main.py 与 gba_input.py 的唯一真源）
# ------------------------------------------------------------------
# 为什么单独一个文件：按键有"画在哪里"和"哪里算按下"两件事，
# 分别由 main.py（绘制）和 gba_input.py（触摸命中）负责。
# 如果两边各存一份坐标表，只要有一边改了、或 LAYOUT 不一致，
# 就会出现"看得见的按钮按不动、按得中的地方没按钮"的错位，
# 而且不会报错。把布局与区域集中到这里，两边都 import，就不可能不一致。
#
# ⚠️ 屏幕与缓冲是两套坐标，别混：
#   * 显示缓冲恒为 720x480（DISP_W x DISP_H），行距 720（不可改，见 main.py 说明）
#   * 缓冲整体显示在屏幕 x = (800-720)//2 = 40 处
#   * BUTTON_REGIONS 用的是**屏幕**坐标（触摸事件也是屏幕坐标）
# ==================================================================

LCD_W, LCD_H = 800, 480
DISP_W, DISP_H = 720, 480
DISP_SCREEN_X = (LCD_W - DISP_W) // 2       # 40

# "full"  —— 3 倍缩放铺满缓冲（游戏画面 = 缓冲内 0..720 = 屏幕 40..760）
# "split" —— 2 倍缩放到缓冲内左侧（游戏画面 = 缓冲内 0..480 = 屏幕 40..520），
#            右侧 520..760 留白给虚拟按键，完全不遮画面
LAYOUT = "full"

# 两种布局的缩放参数，供固件 gba.scale_into(scale, off_x, off_y) 使用。
# off_* 是**缓冲内**坐标。
LAYOUT_SCALE = {
    # layout: (scale, off_x, off_y)
    "full":  (3, 0, 0),
    "split": (2, 0, (DISP_H - 160 * 2) // 2),   # 320 高 -> y=80 垂直居中
}

# 虚拟按键区域：**屏幕坐标** (name, x, y, w, h)
# 这两个表同时被 main.py（绘制）和 gba_input.py（触摸命中）使用，
# 所以不存在"画的表和判定的表不一致"的可能。
BUTTON_REGIONS = {
    # full：肩键左右对称，动作键右下斜排，菜单键底部居中。
    # 左侧不画十字键：方向只由实体摇杆控制。
    "full": [
        ("L",       56, 16, 96, 45),
        ("R",      648, 16, 96, 45),
        ("A",      657, 299, 87, 87),
        ("B",      557, 369, 87, 87),
        ("Select", 276, 421, 112, 41),
        ("Start",  412, 421, 112, 41),
    ],
    # split：按键全部落在右侧留白区。
    # 游戏画面是屏幕 x 40..520（缓冲内 0..480 加上 40 的显示偏移），
    # 所以控制区是 520..760（240 宽）。下面用两列排布，全部 >= 525，
    # 保证**没有任何按键压到游戏画面**（host_py_logic_test 会校验这一点）。
    "split": [
        ("L",      536, 36, 96, 45),
        ("R",      648, 36, 96, 45),
        ("A",      648, 171, 87, 87),
        ("B",      540, 253, 87, 87),
        ("Select", 536, 405, 96, 41),
        ("Start",  648, 405, 96, 41),
    ],
}


def layout_params(layout=None):
    """返回 (scale, off_x, off_y, disp_x, disp_y)。

    scale/off_x/off_y 交给固件 gba.scale_into()；
    disp_x/disp_y 是整块 720x480 图像显示到屏幕的位置。
    """
    name = layout or LAYOUT
    scale, off_x, off_y = LAYOUT_SCALE[name]
    return scale, off_x, off_y, DISP_SCREEN_X, 0


def button_regions(layout=None):
    """返回当前布局的 (name, x, y, w, h) 列表（屏幕坐标）。"""
    return BUTTON_REGIONS[layout or LAYOUT]


def button_contains(name, x, y, w, h, px, py):
    """圆形 A/B、胶囊形其它键；触摸与绘图使用相同的轮廓。"""
    if not (x <= px < x + w and y <= py < y + h):
        return False
    radius = (h - 1) // 2
    cy = y + radius
    if name in ("A", "B"):
        cx = x + (w - 1) // 2
    elif px < x + radius:
        cx = x + radius
    elif px > x + w - 1 - radius:
        cx = x + w - 1 - radius
    else:
        return True
    return (px - cx) ** 2 + (py - cy) ** 2 <= radius ** 2


def _button_shape(target, name, x, y, w, h, color):
    radius = (h - 1) // 2
    cy = y + radius
    if name in ("A", "B"):
        target.draw_circle(x + (w - 1) // 2, cy, radius,
                           color=color, fill=True)
    else:
        # 精确厂商 API 没有圆角矩形，用中段矩形和两端圆组成胶囊。
        target.draw_rectangle(x + radius, y, w - 2 * radius, h,
                              color=color, fill=True)
        target.draw_circle(x + radius, cy, radius, color=color, fill=True)
        target.draw_circle(x + w - 1 - radius, cy, radius,
                           color=color, fill=True)


def draw_controls(target, layout=None):
    """一次性画完整透明 OSD；不写游戏 RGB565 缓冲，不逐帧重画。

    厂商 ARGB8888 颜色是 (A,R,G,B)，clear() 的全零背景完全透明。
    圆环和内填色分开写入，避免几何拼接处反复混合 alpha。
    """
    target.clear()
    for name, x, y, w, h in button_regions(layout):
        rim = (172, 209, 225, 242)
        fill = (88, 20, 29, 42)
        if name == "A":
            rim = (192, 151, 222, 241)
            fill = (94, 20, 44, 57)
        elif name == "B":
            rim = (192, 236, 191, 175)
            fill = (94, 50, 29, 34)
        _button_shape(target, name, x, y, w, h, rim)
        _button_shape(target, name, x + 2, y + 2, w - 4, h - 4, fill)
        label = name.upper()
        size = 30 if name in ("A", "B") else (24 if len(name) == 1 else 18)
        # 内置英文字体约 0.6em 宽；按标签长度居中。
        tx = x + (w - len(label) * size * 3 // 5) // 2
        ty = y + (h - size) // 2
        target.draw_string_advanced(tx, ty, size, label,
                                    color=(238, 246, 249, 255))


def _rounded_panel(target, x, y, w, h, radius, color):
    target.draw_rectangle(x + radius, y, w - 2 * radius, h, color=color, fill=True)
    target.draw_rectangle(x, y + radius, w, h - 2 * radius, color=color, fill=True)
    for cx in (x + radius, x + w - 1 - radius):
        for cy in (y + radius, y + h - 1 - radius):
            target.draw_circle(cx, cy, radius, color=color, fill=True)


def draw_volume_window(target, volume):
    """临时音量面板；位置避开两种布局的六键，不清除既有透明层。"""
    value = max(0, min(100, int(volume)))
    x, y, w, h = 238, 110, 324, 142
    _rounded_panel(target, x, y, w, h, 15, (190, 115, 146, 168))
    _rounded_panel(target, x + 2, y + 2, w - 4, h - 4, 13, (218, 14, 23, 34))
    target.draw_string_advanced(x + 24, y + 20, 24, "VOLUME",
                                color=(246, 238, 246, 250))
    target.draw_string_advanced(x + 232, y + 17, 28, "%d%%" % value,
                                color=(246, 128, 222, 192))
    bx, by, bw, bh = x + 24, y + 65, w - 48, 14
    target.draw_rectangle(bx, by, bw, bh, color=(200, 55, 69, 83), fill=True)
    filled = bw * value // 100
    if filled:
        target.draw_rectangle(bx, by, filled, bh, color=(238, 111, 209, 179), fill=True)
    target.draw_string_advanced(x + 24, y + 103, 16, "X - / +     K CLOSE",
                                color=(228, 182, 203, 216))
