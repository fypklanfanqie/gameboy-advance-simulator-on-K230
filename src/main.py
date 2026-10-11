#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==================================================================
# K230 GBA 模拟器 —— 启动入口（厂商新版镜像 / A 槽 RTAPP）
# ------------------------------------------------------------------
# 目标：开机进入 GBA《宝可梦 绿宝石》模拟器，保留厂商内核与屏幕驱动。
#
# 与历史版本的差别（重要）：
#   * 默认 BOOT_MODE="gba"，直接进模拟器；不再默认跑屏幕诊断。
#   * 显示用 Display.show_image + image.Image(..., alloc=4, data=ba) 零拷贝包装
#     gba.disp_bytes()（alloc=4 是 ALLOC_REF；alloc=3 是 VB，需要 phyaddr）。
#   * 触摸按官方 API 用 point.x / point.y / point.event，忽略抬起事件。
#   * 音频用 PyAudio（官方例程 API）。音频失败绝不影响游戏。
#   * 所有启动信息落盘 /sdcard/gba_boot.log（上电早期串口抓不到）。
#
# 已查证的事实（详见 docs/vendor-gba-source-review-2026-10-10.md）：
#   * 上电会自动执行 /sdcard/main.py；machine.reset() 是软复位，不会重跑 main.py，
#     验证改动必须冷启动（断电再上电）。
#   * 本内核没有用户态背光控制（kd_display_control_backlight 是空桩），
#     gba.backlight() 会返回 -2；不要靠它调亮度。
#   * 资源释放顺序不能变：Display.deinit → exitpoint → sleep → MediaManager.deinit。
# ==================================================================
import os
import sys
import time

import image as _image

# 顶层 import 失败会让 main.py 静默早退（历史上"屏幕啥都没有"的原因之一），
# 所以这几个 import 都用 try 包住，并在 main() 里给出明确反馈。
_DISPLAY_ERR = None
try:
    from media.display import Display
    from media.media import MediaManager
except Exception as _e:      # pragma: no cover - board only
    _DISPLAY_ERR = _e
    Display = None
    MediaManager = None

# 布局与按键区域来自共享模块 gba_ui.py。它缺失/损坏也必须能被诊断出来，
# 否则就会变成"上电什么都没发生"。
_UI_ERR = None
try:
    from gba_ui import (
        LCD_W, LCD_H, DISP_W, DISP_H,
        LAYOUT, layout_params, button_regions, draw_controls, draw_volume_window,
    )
except Exception as _e:      # pragma: no cover - board only
    _UI_ERR = _e
    LCD_W, LCD_H, DISP_W, DISP_H = 800, 480, 720, 480
    LAYOUT = "full"

    def layout_params(layout=None):
        return 3, 0, 0, (LCD_W - DISP_W) // 2, 0

    def button_regions(layout=None):
        return []

    def draw_controls(target, layout=None):
        raise RuntimeError("gba_ui unavailable")

    def draw_volume_window(target, volume):
        raise RuntimeError("gba_ui unavailable")

# ====================== 配置区 ======================
# 启动模式：
#   "gba"      —— 正常玩游戏（默认）
#   "selftest" —— 只做模块/显示自检并打印信息，不加载 ROM
#   "none"     —— 立刻退出到 REPL
BOOT_MODE = "gba"

ROM_PATH = "/sdcard/roms/pokemon_emerald.gba"
SAVE_PATH = "/sdcard/roms/pokemon_emerald.sav"
STATE_PATH = "/sdcard/roms/pokemon_emerald.state"
LOG_PATH = "/sdcard/gba_boot.log"
AUDIO_PROBE_ONCE_PATH = "/sdcard/audio_probe.once"

# 布局与虚拟按键区域集中在 src/gba_ui.py（绘制与触摸判定共用同一张表）。
# LAYOUT 在 gba_ui.py 里改："full"（3 倍铺满）或 "split"（2 倍 + 右侧控制区）。
#
# ⚠️ 重要：无论哪种布局，**显示缓冲始终是 720x480、行距 720**，
#    Python 侧也必须始终用 image.Image(720, 480, ...) 去包装它。
#    原因：Show_image 的步长取自 image->w（modmedia.display.cpp：
#    buffer->vf_info.v_frame.stride[0] = image->w * bpp）。
#    如果按 480 宽去包装同一块缓冲，行距就对不上，画面会被斜切；
#    更糟的是 py_image_alloc 只检查 size > buflen（307200 < 691200 会通过），
#    所以这是**静默出错**。
# 布局差异通过固件的 gba.scale_into(倍率, off_x, off_y) 实现，
# 它在**不改行距**的前提下把画面缩放并放到缓冲内的指定位置。
# （LCD_W/DISP_W/LAYOUT/layout_params/button_regions 已在文件顶部从 gba_ui 导入。）

TO_IDE = False          # 脱机运行必须 False（True 会把画面回传 IDE）
AUDIO_ENABLE = True     # 音频失败会自动降级，不影响游戏
AUDIO_RATE = 44100      # 必须与 gba.set_audio_rate() 一致（官方例程用 44100）
FORCE_KEYS = None       # 调试用：固定按键掩码（None=正常读输入）
SHOW_CONTROLS = True    # 半透明六键独立 OSD，仅启动绘制一次；方向由实体摇杆控制
SHOW_FPS_ONSCREEN = False  # 在画面左上角叠显帧率（同样有每帧绘制开销）

# 摇杆 K 键打开音量窗口；窗口打开时 X 轴左右调节音量。
VOLUME_STEP = 5
VOLUME_TIMEOUT_MS = 2500
VOLUME_REPEAT_DELAY_MS = 400
VOLUME_REPEAT_MS = 150

# 真机性能剖析：每 120 帧记录一次 run/scale/show 的平均微秒数。
# ticks_us() 只包住已有的 C 调用，不改变帧循环顺序；完成定位后可关闭。
PROFILE_TIMING = True

# 帧率上限：GBA 是 59.7275 Hz。模拟器本身不等垂直同步，跑多快取决于性能，
# 所以必须自己做节流，否则游戏会快放（音乐/计时全乱）。
TARGET_FPS = 59.7275
FRAME_MS = 1000.0 / TARGET_FPS          # ≈16.74 ms
# 单帧最多补偿多少毫秒（落后太多时不要一次追到底，避免"快进"）
MAX_CATCHUP_MS = 100

# 帧率日志间隔（帧）
FPS_LOG_EVERY = 120
# 每隔多少帧把电池存档写盘一次（0=只在退出时写）
SAVE_EVERY_FRAMES = 1800

# 显示缓冲尺寸、布局参数与按键区域都来自 gba_ui（见上面的 import）。


# 每次写日志都 open→write→close：避免句柄长期占用（否则事后 REPL 读会 EPERM）
try:
    _f = open(LOG_PATH, "w")
    _f.close()
except Exception:
    pass

_log_failures = 0


def log(msg):
    """打印并落盘。上电阶段的串口输出会丢，落盘才能事后取证。"""
    global _log_failures
    line = "[gba] %s" % msg
    print(line)
    try:
        f = open(LOG_PATH, "a")
        f.write(line + "\n")
        f.close()
    except Exception:
        _log_failures += 1


def log_exception(tag, exc):
    print("[gba] %s: %r" % (tag, exc))
    try:
        f = open(LOG_PATH, "a")
        f.write("[gba] %s: %r\n" % (tag, exc))
        sys.print_exception(exc, f)
        f.close()
    except Exception:
        pass


# ==================================================================
# 固件身份自检
# ==================================================================
def firmware_identity():
    """确认板子跑的是我们构建的固件，并把 gba 模块能力打进日志。"""
    try:
        log("uname: %s" % str(os.uname()))
    except Exception as e:
        log("uname fail: %r" % e)

    try:
        import hello
        log("module hello OK add(1,2)=%s" % hello.add(1, 2))
    except Exception as e:
        log("module hello FAIL: %r" % e)

    try:
        import gba
    except Exception as e:
        log("module gba FAIL: %r" % e)
        return None

    path = getattr(gba, "__file__", "BUILTIN")
    log("module gba OK (%s)" % path)
    if not hasattr(gba, "disp_bytes"):
        log("gba 是 SD 卡上的 stub，不是内置 C 模块：不进入模拟器")
        return None

    try:
        info = gba.info()
        log("gba.info: %s" % str(info))
    except Exception as e:
        log("gba.info fail: %r" % e)

    try:
        log("rom exists: %d bytes" % os.stat(ROM_PATH)[6])
    except Exception as e:
        log("rom missing: %r" % e)

    try:
        log("save exists: %d bytes" % os.stat(SAVE_PATH)[6])
    except Exception as e:
        log("save missing (首次运行正常): %r" % e)

    return gba


# ==================================================================
# 显示
# ==================================================================
_display_overlay = None


def release_display():
    """官方资源释放契约；顺序不能改。"""
    global _display_overlay
    if Display is None:
        return
    try:
        Display.deinit()
    except Exception as e:
        log("Display.deinit 异常: %r" % e)
    # 透明图像保持到显示层停用之后，避免提前释放。
    _display_overlay = None
    try:
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        time.sleep_ms(100)
    except Exception as e:
        log("exitpoint/sleep 异常: %r" % e)
    try:
        MediaManager.deinit()
    except Exception as e:
        log("MediaManager.deinit 异常: %r" % e)


def init_display(tag="", controls=False):
    global _display_overlay
    try:
        Display.init(Display.ST7701, width=LCD_W, height=LCD_H,
                     osd_num=2, to_ide=TO_IDE)
        MediaManager.init()
        _display_overlay = make_canvas()
        if controls:
            draw_controls(_display_overlay)
        else:
            _display_overlay.clear()
        # 横屏 ST7701 共用提交暂存区，大小由第一次 show_image 决定。
        # 先提交完整 800x480 ARGB8888，再显示较小的 RGB565 游戏图，
        # 防止透明层静默截断。菜单和自检也遵守此顺序。
        Display.show_image(_display_overlay, layer=Display.LAYER_OSD1, alpha=255)
        if controls:
            log("虚拟六键已显示: ARGB8888 OSD1，像素半透明，方向=实体摇杆")
        log("Display.init ok %s" % tag)
        return True
    except Exception as e:
        log_exception("Display.init 失败 %s" % tag, e)
        release_display()
        return False


def make_canvas():
    """普通画布（ARGB8888 在 LCD 上可直接用）。"""
    return _image.Image(LCD_W, LCD_H, _image.ARGB8888)


def update_volume_overlay(volume, visible):
    """在独立 OSD1 上绘制音量窗口，并提交一次更新。

    游戏 RGB565 缓冲永远不参与绘制；只有音量状态变化或窗口隐藏时才
    重画并提交 OSD1，因此正常帧循环不会增加逐帧绘制开销。
    """
    if _display_overlay is None or Display is None:
        return
    try:
        if SHOW_CONTROLS:
            draw_controls(_display_overlay)
        else:
            _display_overlay.clear()
        if visible:
            draw_volume_window(_display_overlay, volume)
        Display.show_image(_display_overlay,
                           layer=Display.LAYER_OSD1, alpha=255)
    except Exception as e:
        log_exception("音量窗口绘制失败", e)


def show_message(title, sub=""):
    """不开模拟器时给用户一个可见反馈。"""
    if not init_display("[msg]"):
        return
    img = None
    try:
        img = make_canvas()
        img.clear()
        img.draw_string_advanced(40, 190, 32, title, color=(255, 255, 255))
        if sub:
            img.draw_string_advanced(40, 250, 20, sub, color=(120, 200, 255))
        Display.show_image(img)
        time.sleep_ms(4000)
    except Exception as e:
        log_exception("提示画面失败", e)
    finally:
        img = None
        release_display()


# ==================================================================
# 虚拟按键由 gba_ui.draw_controls 一次性绘制到独立透明层
# ==================================================================
# ==================================================================
# 自检
# ==================================================================
def selftest():
    log("--- 自检开始 ---")
    gba = firmware_identity()
    if gba is None:
        log("--- 自检结束：gba 模块不可用 ---")
        return False

    scale, off_x, off_y, disp_x, disp_y = layout_params()
    log("layout=%s scale=%d off=(%d,%d) disp_at=(%d,%d)"
        % (LAYOUT, scale, off_x, off_y, disp_x, disp_y))

    if not init_display("[selftest]"):
        log("--- 自检结束：显示初始化失败 ---")
        return False

    try:
        # 显示一个已知图形，确认显示通路（不是条纹诊断，只看通路是否活着）
        img = make_canvas()
        img.clear()
        img.draw_string_advanced(30, 40, 30, "GBA selftest", color=(255, 255, 255))
        img.draw_rectangle(30, 100, 200, 80, color=(255, 0, 0), fill=True)
        img.draw_rectangle(250, 100, 200, 80, color=(0, 255, 0), fill=True)
        img.draw_rectangle(470, 100, 200, 80, color=(0, 0, 255), fill=True)
        img.draw_string_advanced(30, 200, 20, "hello.add(1,2) ok -> see log",
                                 color=(200, 200, 200))
        Display.show_image(img)
        time.sleep_ms(1500)

        # 零拷贝显示包装：**必须**用固定的 720x480（见 LAYOUT 处的说明）
        gba.display_alloc()
        ba = gba.disp_bytes()
        log("disp_bytes len=%d (期望 %d)" % (len(ba), DISP_W * DISP_H * 2))
        gimg = _image.Image(DISP_W, DISP_H, _image.RGB565, alloc=4, data=ba)
        log("image.Image 零拷贝包装 OK (alloc=4/REF, %dx%d)" % (DISP_W, DISP_H))

        # 布局缩放也要验一下，避免真机上才发现参数越界
        ret = gba.scale_into(scale, off_x, off_y)
        log("gba.scale_into(%d,%d,%d) -> %s" % (scale, off_x, off_y, ret))

        log("gba.state_size=%s" % gba.state_size())
        log("gba.audio_rate=%s" % gba.audio_rate())
        log("--- 自检结束：全部通过 ---")
        return True
    except Exception as e:
        log_exception("自检异常", e)
        return False
    finally:
        release_display()


# ==================================================================
# 模拟器主循环
# ==================================================================
def save_if_idle(gba, tag):
    """安全落盘电池存档。

    为什么不能直接 save_write：
    本构建里核心的 savedata 没有绑定 VFile（GBASavedataInit 传 NULL），
    GBASavedataClean() 因此在第一行就返回 —— 存档的 dirty 标志永远不清、
    也没有任何后台逻辑把存档刷盘。落盘完全靠这里调用 save_write()。

    而游戏写 FLASH 时（扇区擦除/编程）会排一个内部定时事件，这段时间里
    savedata.data 处于"半写"状态（例如整扇区已被填成 0xFF、新数据还没写回）。
    此刻抓快照就可能把半成品存进 .sav，覆盖掉用户的有效进度。

    所以先查 gba.save_busy()：忙着就跳过这一轮，下一轮再存。
    这与 mGBA 自己判断"忙"用的是同一个条件（mTimingIsScheduled(dust)）。
    """
    try:
        if gba.save_busy() == 1:
            log("存档正忙（%s），跳过本次保存" % tag)
            return None
    except Exception:
        # 老固件没有这个函数时不阻塞保存
        pass
    try:
        n = gba.save_write(SAVE_PATH)
        log("存档已保存（%s）: %s" % (tag, n))
        return n
    except Exception as e:
        log("存档保存失败（%s）: %r" % (tag, e))
        return None


def run_emulator(gba):
    # ---- 输入 ----
    inp = None
    try:
        import gba_input
        inp = gba_input.GBAInput()
        log("输入模块已加载 touch_async=%s k_gpio18=%s"
            % (getattr(inp, "touch_async", False), getattr(inp, "k_enabled", False)))
    except Exception as e:
        log_exception("输入模块不可用（只能看画面）", e)
    try:
        _run_emulator(gba, inp)
    finally:
        if inp is not None:
            try:
                close = getattr(inp, "deinit", None)
                if close is not None:
                    close()
            except Exception as e:
                log("输入释放失败: %r" % e)


def _run_emulator(gba, inp):

    # ---- 音频（可失败）----
    # 必须在 init_display()（内部会 MediaManager.init()）之后创建：
    # PyAudio 的 Write_stream.start_stream() 会立刻 kd_mpi_vb_create_pool()，
    # 而 VB 池依赖 MediaManager 已初始化（官方例程也是先 MediaManager.init()
    # 再 p.open(...)）。
    audio = None

    # ---- 加载 ROM ----
    log("加载 ROM: %s" % ROM_PATH)
    try:
        ret = gba.load(ROM_PATH)
    except Exception as e:
        log_exception("gba.load 异常", e)
        show_message("gba.load crashed", str(e)[:60])
        return
    if ret != 0:
        log("ROM 加载失败: %d" % ret)
        show_message("ROM load failed: %d" % ret, ROM_PATH)
        return
    log("ROM 加载成功")

    # 存档类型由 ROM 的 game code 决定，在 reset() 里应用（mGBA 的 override 表里
    # BPEE = SAVEDATA_FLASH1M）。把探测结果打进日志，便于真机判断存档是否可用。
    try:
        log("save_type=%s save_size=%d" % (gba.save_type(), gba.save_size()))
    except Exception as e:
        log("读取存档类型失败: %r" % e)

    # ---- 恢复存档（失败不致命） ----
    # 必须在 load() 之后：reset() 里才会按 game code 定出类型并分配存档缓冲。
    try:
        n = gba.save_load(SAVE_PATH)
        log("存档恢复: %s" % n)
    except Exception as e:
        log("存档恢复失败（继续）: %r" % e)

    # ---- 显示 ----
    scale, off_x, off_y, disp_x, disp_y = layout_params()
    if not init_display("[emu]", controls=SHOW_CONTROLS):
        log("显示初始化失败，退出")
        try:
            gba.unload()
        except Exception:
            pass
        return

    # ---- 音频（可失败；必须在 MediaManager.init() 之后）----
    if AUDIO_ENABLE:
        try:
            import gba_audio
            audio = gba_audio.GBAudio(AUDIO_RATE)
            if not audio.init():
                audio = None
        except Exception as e:
            log_exception("音频初始化失败（静音继续）", e)
            audio = None

    # 固件侧 blip 输出率必须与板端 PyAudio 一致，否则音调/速度都不对
    try:
        applied = gba.set_audio_rate(AUDIO_RATE)
        log("gba.set_audio_rate(%d) -> %s" % (AUDIO_RATE, applied))
    except Exception as e:
        log("set_audio_rate 失败: %r" % e)

    gimg = None
    try:
        gba.display_alloc()
        ba = gba.disp_bytes()
        # 始终按固定的 720x480 包装：Show_image 的步长取自 image->w，
        # 用别的宽度会让画面斜切（而且是静默出错）。布局差异由 scale_into 负责。
        gimg = _image.Image(DISP_W, DISP_H, _image.RGB565, alloc=4, data=ba)
        log("缩放缓冲包装 OK: %d 字节 (%dx%d)" % (len(ba), DISP_W, DISP_H))
    except Exception as e:
        log_exception("缩放缓冲包装失败", e)
        if audio is not None:
            try:
                audio.deinit()
            except Exception:
                pass
        try:
            gba.unload()
        except Exception:
            pass
        release_display()
        return

    log("进入主循环（Ctrl+C 退出）")
    frame = 0
    fps_t0 = time.ticks_ms()
    profiling = PROFILE_TIMING and hasattr(time, "ticks_us")
    prof_t0 = time.ticks_us() if profiling else 0
    prof_input_us = 0
    prof_run_us = 0
    prof_scale_us = 0
    prof_show_us = 0
    prof_audio_us = 0
    prof_work_us = 0
    # 帧率节流基准（见循环末尾的节流逻辑）
    next_deadline = time.ticks_ms()
    frame_fraction_ms = 0.0
    key_fn = getattr(gba, "set_keys", None) or getattr(gba, "keys", None)
    last_mask = -1
    volume_visible = False
    volume_level = getattr(audio, "volume_level", 80) if audio is not None else 80
    volume_last_activity = time.ticks_ms()
    volume_horizontal_mask = (1 << 4) | (1 << 5)
    volume_x_dir = 0
    volume_next_repeat = volume_last_activity
    try:
        while True:
            if profiling:
                _frame_t = time.ticks_us()
            os.exitpoint()

            # 输入
            mask = FORCE_KEYS
            x_dir = 0
            k_pressed = False
            if mask is None and inp is not None:
                try:
                    read_events = getattr(inp, "read_events", None)
                    if read_events is not None:
                        event = read_events()
                        if not isinstance(event, dict):
                            event = {}
                        mask = event.get("mask", 0)
                        x_dir = event.get("x_dir", 0)
                        k_pressed = bool(event.get("k_pressed", False))
                    else:
                        mask = inp.read_keys()
                except Exception:
                    mask = 0
            overlay_dirty = False
            if FORCE_KEYS is None and k_pressed:
                volume_visible = not volume_visible
                volume_last_activity = time.ticks_ms()
                volume_x_dir = 0
                overlay_dirty = True
            if FORCE_KEYS is None and volume_visible:
                now_volume = time.ticks_ms()
                # 初次横推或反向立即走一步，持续横推先等400ms，再每150ms
                # 重复一步。回中立会重置，避免每帧5级瞬间拉满音量。
                if x_dir:
                    volume_last_activity = now_volume
                    volume_step = x_dir != volume_x_dir
                    if volume_step:
                        volume_next_repeat = time.ticks_add(now_volume, VOLUME_REPEAT_DELAY_MS)
                    elif time.ticks_diff(now_volume, volume_next_repeat) >= 0:
                        volume_step = True
                        volume_next_repeat = time.ticks_add(now_volume, VOLUME_REPEAT_MS)
                    if volume_step:
                        new_volume = max(0, min(100, volume_level + x_dir * VOLUME_STEP))
                        if new_volume != volume_level:
                            if audio is None or audio.set_volume(new_volume):
                                volume_level = new_volume
                                overlay_dirty = True
                volume_x_dir = x_dir
                mask &= ~volume_horizontal_mask
                if time.ticks_diff(now_volume, volume_last_activity) >= VOLUME_TIMEOUT_MS:
                    volume_visible = False
                    volume_x_dir = 0
                    overlay_dirty = True
            if overlay_dirty:
                update_volume_overlay(volume_level, volume_visible)
            if mask is not None and key_fn is not None and mask != last_mask:
                try:
                    key_fn(mask)
                    last_mask = mask
                except Exception:
                    pass

            if profiling:
                prof_input_us += time.ticks_diff(time.ticks_us(), _frame_t)

            # 全屏缩放覆盖整块缓冲；其它布局由缩放器清理留白。
            if profiling:
                _t = time.ticks_us()
                gba.run_frame()
                prof_run_us += time.ticks_diff(time.ticks_us(), _t)
                _t = time.ticks_us()
                gba.scale_into(scale, off_x, off_y)
                prof_scale_us += time.ticks_diff(time.ticks_us(), _t)
            else:
                gba.run_frame()
                gba.scale_into(scale, off_x, off_y)

            if profiling:
                _t = time.ticks_us()
                Display.show_image(gimg, x=disp_x, y=disp_y, layer=Display.LAYER_OSD0)
                prof_show_us += time.ticks_diff(time.ticks_us(), _t)
            else:
                Display.show_image(gimg, x=disp_x, y=disp_y, layer=Display.LAYER_OSD0)

            # 音频：本帧新产生的 PCM 先收进累积缓冲，攒够整块才写出
            if audio is not None:
                try:
                    if profiling:
                        _t = time.ticks_us()
                    audio.pump(gba)
                    if profiling:
                        prof_audio_us += time.ticks_diff(time.ticks_us(), _t)
                except Exception as e:
                    log("音频异常，关闭音频: %r" % e)
                    try:
                        audio.deinit()
                    except Exception:
                        pass
                    audio = None

            if profiling:
                prof_work_us += time.ticks_diff(time.ticks_us(), _frame_t)
            frame += 1

            # ---- 帧率节流 ----
            # 跑得比 59.7275 FPS 快时才 sleep；跑不到时**绝不 sleep**，
            # 把所有 CPU 时间留给模拟（否则进一步拉低帧率）。
            # 落后超过 MAX_CATCHUP_MS 说明性能不足，重置基准避免"快进"。
            # 保留 16.74ms 的小数余量，交替等待 16/17ms，避免截断成 62.5Hz。
            frame_fraction_ms += FRAME_MS
            frame_step_ms = int(frame_fraction_ms)
            frame_fraction_ms -= frame_step_ms
            next_deadline = time.ticks_add(next_deadline, frame_step_ms)
            now = time.ticks_ms()
            slack = time.ticks_diff(next_deadline, now)
            if slack > 0:
                time.sleep_ms(slack)
            elif slack < -MAX_CATCHUP_MS:
                next_deadline = now

            if frame % FPS_LOG_EVERY == 0:
                dt = time.ticks_diff(time.ticks_ms(), fps_t0)
                fps = FPS_LOG_EVERY * 1000.0 / dt if dt else 0
                if audio is not None:
                    st = audio.stats()
                    log("frame %d, %.1f FPS (目标 %.1f) 音频 blocks=%d failed=%d dropped=%d pending=%d pcm_nz=%d pcm_zero=%d last_rc=%r started=%s prebuffer=%d gap_max_us=%d write_max_us=%d vol=%r"
                        % (frame, fps, TARGET_FPS, st["blocks_written"],
                           st.get("blocks_failed", 0), st["blocks_dropped"],
                           st["pending_bytes"], st.get("pcm_nonzero_chunks", 0),
                           st.get("pcm_zero_chunks", 0), st.get("last_write_rc"),
                           st.get("started", False), st.get("prebuffer_blocks", 0),
                           st.get("max_send_gap_us", 0), st.get("max_write_us", 0),
                           st.get("volume_read")))
                else:
                    log("frame %d, %.1f FPS (目标 %.1f)" % (frame, fps, TARGET_FPS))
                if frame == FPS_LOG_EVERY:
                    try:
                        log("core runtime: %s" % gba.info())
                    except Exception:
                        pass
                if profiling:
                    pdt = time.ticks_diff(time.ticks_us(), prof_t0)
                    denom = FPS_LOG_EVERY
                    log("timing frame %d: input=%.2fms run=%.2fms scale=%.2fms show=%.2fms audio=%.2fms work=%.2fms total=%.2fms"
                        % (frame, prof_input_us / denom / 1000.0,
                           prof_run_us / denom / 1000.0,
                           prof_scale_us / denom / 1000.0,
                           prof_show_us / denom / 1000.0,
                           prof_audio_us / denom / 1000.0,
                           prof_work_us / denom / 1000.0,
                           pdt / denom / 1000.0))
                    prof_t0 = time.ticks_us()
                    prof_input_us = 0
                    prof_run_us = 0
                    prof_scale_us = 0
                    prof_show_us = 0
                    prof_audio_us = 0
                    prof_work_us = 0
                fps_t0 = time.ticks_ms()

            if SAVE_EVERY_FRAMES and frame % SAVE_EVERY_FRAMES == 0:
                save_if_idle(gba, "定期")

    except KeyboardInterrupt:
        log("用户停止")
    except Exception as e:
        log_exception("主循环异常", e)
    finally:
        if audio is not None:
            try:
                audio.deinit()
            except Exception:
                pass
        # 退出时先等存档不忙再写，避免把半写状态存盘
        for _try in range(120):
            try:
                if gba.save_busy() != 1:
                    break
            except Exception:
                break
            time.sleep_ms(16)
        save_if_idle(gba, "退出")
        try:
            gba.unload()
        except Exception:
            pass
        gimg = None
        release_display()


def run_audio_probe_once():
    """标记只触发一次左右声道对照；完整释放后照常进入游戏。"""
    try:
        os.stat(AUDIO_PROBE_ONCE_PATH)
    except OSError:
        return False
    try:
        # 先消费标记；诊断失败也不会每次启动反复阻塞进入游戏。
        os.remove(AUDIO_PROBE_ONCE_PATH)
        log("耳机声道测试：左、右、双耳同相、双耳反相各3秒，段间半秒静音，再进入游戏")
        import audio_tone_probe
        result = audio_tone_probe.run_channels(seconds_per_stage=3)
        if result.get("error") or result.get("failed") or result.get("cleanup_errors"):
            log("耳机测试报告异常（继续游戏）: %r" % result)
            return False
        log("耳机测试结束，准备进入游戏")
        return True
    except Exception as e:
        log_exception("一次性音频测试失败（继续游戏）", e)
        return False


def main():
    os.exitpoint(os.EXITPOINT_ENABLE)
    log("=== main.py 启动 (BOOT_MODE=%s) ===" % BOOT_MODE)
    log("uptime_ms=%d" % time.ticks_ms())

    if _DISPLAY_ERR is not None:
        log("media.display/media.media 导入失败: %r" % _DISPLAY_ERR)
        log("=== main.py 结束（显示模块不可用）===")
        return

    if _UI_ERR is not None:
        log("gba_ui 导入失败: %r" % _UI_ERR)
        log("（gba_ui.py 是否已拷到 /sdcard？它是布局与按键的唯一真源）")
        log("=== main.py 结束（gba_ui 不可用）===")
        return

    if BOOT_MODE == "none":
        log("=== main.py 结束（none）===")
        return

    if BOOT_MODE == "selftest":
        selftest()
        log("=== main.py 结束（selftest）===")
        return

    gba = firmware_identity()
    if gba is None:
        show_message("GBA module missing", "flash the matched RTAPP")
        log("=== main.py 结束（无 gba 模块）===")
        return

    if AUDIO_ENABLE:
        run_audio_probe_once()
    run_emulator(gba)
    log("=== main.py 结束 ===")


main()
