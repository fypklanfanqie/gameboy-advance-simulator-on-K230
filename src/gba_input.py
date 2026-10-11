#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==================================================================
# K230 GBA 输入映射（厂商新版固件）
# ------------------------------------------------------------------
# 摇杆 ADC（32 脚 ADC0 = X，36 脚 ADC1 = Y，K=22脚 IO18）→ GBA 十字键/音量键
# 触摸六键（A/B/L/R/Start/Select）→ GBA 按键掩码
#
# 已按厂商源码核对的事实（见 docs/vendor-gba-source-review-2026-10-10.md）：
#   * machine_adc.c: machine_adc_read_u16() 返回 drv_adc_read(channel) & 0xFFFF，
#     而 drv_adc_read 在出错时返回 __UINT32_MAX__ → 掩码后是 65535。
#     所以 65535 必须当作"读失败"丢弃，不能当成摇杆打到底。
#   * ADC 是 12 bit（drv_adc.h: DRV_ADC_RESOLUTION = 4096-1）。
#   * machine_touch.c 的 TOUCH.read() 返回的是 OBJECT 元组，
#     要用 point.x / point.y / point.event，不能按 tuple 解包。
#     事件常量：EVENT_NONE=0, EVENT_UP=1, EVENT_DOWN=2, EVENT_MOVE=3。
#   * TOUCH 的 rotate 参数决定坐标是否旋转（ROTATE_0/90/180/270/SWAP_XY），
#     本模块按 ROTATE_0 假设，并对 x/y 做边界夹取以防越界。
#
# 摇杆轴向/阈值仍需真机标定：ADC_* 阈值是可用起点，不是已验证结论。
# 设 JOY_DEBUG=True 会把每次采样写进日志，便于一次性标定。
# ==================================================================
from machine import ADC, TOUCH, Pin, FPIOA

# GBA 按键位掩码 = bit(GBA_KEY_i)，与固件导出的 gba.GBA_KEY_* 一致
BTN_A      = 1 << 0    # 0x0001
BTN_B      = 1 << 1    # 0x0002
BTN_SELECT = 1 << 2    # 0x0004
BTN_START  = 1 << 3    # 0x0008
BTN_RIGHT  = 1 << 4    # 0x0010
BTN_LEFT   = 1 << 5    # 0x0020
BTN_UP     = 1 << 6    # 0x0040
BTN_DOWN   = 1 << 7    # 0x0080
BTN_R      = 1 << 8    # 0x0100
BTN_L      = 1 << 9    # 0x0200

# ---- 摇杆标定参数（需真机确认）----
ADC_MAX = 4095          # 12 bit
ADC_ERR = 0xFFFF        # drv_adc_read 失败经 &0xFFFF 后的值
ADC_DEADZONE = 600      # 中心死区（±）
JOY_INVERT_X = False    # 若左右反了，改 True
JOY_INVERT_Y = True     # 当前实物摇杆上下反向，已按用户反馈反转
JOY_SWAP_XY = False     # 若 X/Y 接反了，改 True
JOY_DEBUG = False       # True=把采样写进 /sdcard/gba_boot.log
JOY_K_ENABLE = True     # HS-S34-L K 线接 40PIN 第22脚（IO18），按下为低电平
JOY_K_PIN = 18
JOY_K_DEBOUNCE_READS = 2

# ---- 触摸六键区域 ----
# 区域表与布局参数集中在 src/gba_ui.py（"画键"和"判定触摸"共用同一张表），
# 因此不存在两边坐标不一致、或 LAYOUT 切换后触摸区不跟着变的错位问题。
from gba_ui import button_regions, button_contains, LAYOUT as _UI_LAYOUT

# 名称 -> GBA 按键位
_BTN_BY_NAME = {
    "A": BTN_A, "B": BTN_B, "Select": BTN_SELECT, "Start": BTN_START,
    "L": BTN_L, "R": BTN_R,
}

# (name, x, y, w, h, mask) —— 屏幕坐标，直接取自共享表
TOUCH_REGIONS = [
    (name, x, y, w, h, _BTN_BY_NAME[name])
    for (name, x, y, w, h) in button_regions(_UI_LAYOUT)
]

# 触摸事件（machine_touch.c 导出的常量值）
EVENT_NONE = 0
EVENT_UP = 1
EVENT_DOWN = 2
EVENT_MOVE = 3

LOG_PATH = "/sdcard/gba_boot.log"


def _dbg(msg):
    if not JOY_DEBUG:
        return
    try:
        f = open(LOG_PATH, "a")
        f.write("[gba] joy %s\n" % msg)
        f.close()
    except Exception:
        pass


class GBAInput:
    """摇杆 + 触摸 → GBA 按键掩码。"""

    def __init__(self):
        self.adc_x = ADC(0)   # 32 脚
        self.adc_y = ADC(1)   # 36 脚
        self.k_pin = None
        self.k_enabled = False
        self._k_candidate = False
        self._k_stable = False
        self._k_candidate_reads = 0
        if JOY_K_ENABLE:
            try:
                fpioa = FPIOA()
                fpioa.set_function(JOY_K_PIN, FPIOA.GPIO0 + JOY_K_PIN, ie=1, oe=0)
                try:
                    self.k_pin = Pin(JOY_K_PIN, Pin.IN, pull=Pin.PULL_UP, drive=7)
                except TypeError:
                    self.k_pin = Pin(JOY_K_PIN, Pin.IN, Pin.PULL_UP)
                self.k_enabled = True
            except Exception:
                self.k_pin = None
        self.tp = TOUCH(0)
        self._dbg_count = 0
        self.touch_async = False
        self._touch_read = self.tp.read
        # 新用户态固件由原生线程读取触摸；主循环只取缓存，不等 33ms。
        start = getattr(self.tp, "start_async", None)
        cached = getattr(self.tp, "read_cached", None)
        if start is not None and cached is not None:
            try:
                start()
                self._touch_read = lambda: cached(10)
                self.touch_async = True
            except Exception:
                # 旧固件或线程创建失败仍可操作；启动日志显示降级状态。
                pass

    # ---------------- 摇杆 ----------------
    def _read_joystick_state(self):
        """读摇杆 → 十字键掩码。任一轴读失败时该轴不产生方向。"""
        mask = 0
        x_dir = 0
        y_dir = 0
        try:
            raw_x = self.adc_x.read_u16()
        except Exception:
            raw_x = ADC_ERR
        try:
            raw_y = self.adc_y.read_u16()
        except Exception:
            raw_y = ADC_ERR

        if JOY_SWAP_XY:
            raw_x, raw_y = raw_y, raw_x

        if raw_x != ADC_ERR:
            vx = raw_x
            if JOY_INVERT_X:
                vx = ADC_MAX - vx
            if vx < ADC_MAX // 2 - ADC_DEADZONE:
                mask |= BTN_LEFT
                x_dir = -1
            elif vx > ADC_MAX // 2 + ADC_DEADZONE:
                mask |= BTN_RIGHT
                x_dir = 1

        if raw_y != ADC_ERR:
            vy = raw_y
            if JOY_INVERT_Y:
                vy = ADC_MAX - vy
            if vy < ADC_MAX // 2 - ADC_DEADZONE:
                mask |= BTN_UP
                y_dir = -1
            elif vy > ADC_MAX // 2 + ADC_DEADZONE:
                mask |= BTN_DOWN
                y_dir = 1

        if JOY_DEBUG:
            self._dbg_count += 1
            if self._dbg_count % 30 == 0:
                _dbg("x=%d y=%d mask=0x%03X" % (raw_x, raw_y, mask))
        return mask, x_dir, y_dir

    def read_joystick(self):
        return self._read_joystick_state()[0]

    def _read_k_pressed(self):
        if not self.k_enabled or self.k_pin is None:
            return False
        try:
            raw_pressed = self.k_pin.value() == 0
        except Exception:
            return False
        if raw_pressed != self._k_candidate:
            self._k_candidate = raw_pressed
            self._k_candidate_reads = 1
        else:
            self._k_candidate_reads += 1
        if (self._k_candidate_reads >= JOY_K_DEBOUNCE_READS and
                self._k_stable != self._k_candidate):
            self._k_stable = self._k_candidate
            return self._k_stable
        return False

    # ---------------- 触摸 ----------------
    def read_touch(self):
        """读触摸六键 → 按键掩码。

        官方 API 是 OBJECT 属性访问（point.x / point.y / point.event），
        不是 tuple 解包。抬起事件必须忽略，否则松手瞬间还会多按一帧。
        """
        mask = 0
        try:
            points = self._touch_read()
        except Exception:
            return 0
        if not points:
            return 0

        for p in points:
            try:
                event = p.event
            except Exception:
                event = EVENT_DOWN
            if event == EVENT_UP:
                continue
            try:
                px = p.x
                py = p.y
            except Exception:
                # 兼容极少数按 tuple 暴露的实现
                try:
                    px, py = p[0], p[1]
                except Exception:
                    continue
            # 越界值直接丢弃（旋转配置不符时可能出现）
            if not (0 <= px < 800 and 0 <= py < 480):
                continue
            for _name, rx, ry, rw, rh, btn in TOUCH_REGIONS:
                if button_contains(_name, rx, ry, rw, rh, px, py):
                    mask |= btn

        if JOY_DEBUG and mask:
            _dbg("touch mask=0x%03X" % mask)
        return mask

    # ---------------- 汇总 ----------------
    def read_keys(self):
        return self.read_events()["mask"]

    def read_events(self):
        joystick, x_dir, y_dir = self._read_joystick_state()
        return {"mask": joystick | self.read_touch(), "x_dir": x_dir,
                "y_dir": y_dir, "k_pressed": self._read_k_pressed()}

    def deinit(self):
        # TOUCH.deinit() 先停止并 join 原生线程，再关闭触摸设备。
        if self.tp is not None:
            close = getattr(self.tp, "deinit", None)
            if close is not None:
                close()
            self.tp = None
            self.touch_async = False
            self._touch_read = lambda: ()
