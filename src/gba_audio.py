#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==================================================================
# K230 GBA 音频输出（厂商新版固件）
# ------------------------------------------------------------------
# mGBA PSG → blip → 交错 int16 LE 立体声 → PyAudio → 3.5mm 耳机口
#
# ── 已按厂商源码核对的事实 ────────────────────────────────────────
# * 官方音频例程（教程 3.3 Audio实验/audio.py）用 PyAudio：
#       p = PyAudio(); p.initialize(CHUNK); MediaManager.init()
#       stream = p.open(format=paInt16, channels=2, rate=RATE,
#                       output=True, frames_per_buffer=CHUNK)
#       stream.write(data)
#   其中 RATE = 44100，CHUNK = 44100 // 25 = 1764（40ms）。
# * builtin_py/media/pyaudio.py 的 Write_stream.write() 实现是：
#       uctypes.bytearray_at(virt_addr, frame.len)[:] = data
#       kd_mpi_ao_send_frame(dev, chn, frame, 1000)
#   两个关键点：
#     (a) bytearray_at() 返回的是**定长引用型** bytearray（free=0，
#         len=frame.len）；切片赋值时 MicroPython 走
#         mp_seq_replace_slice_no_grow + mp_seq_clear，
#         所以 **数据比 frame.len 短时不会报错，而是把尾部补零**；
#     (b) 真正发声的长度是 `frame.len`（= frames_per_buffer*声道*2），
#         与本次传入多少字节无关。
#   ⇒ 每次 write() 一定消耗 frames_per_buffer 个样本的播放时间。
#
# ── 这带来一个必须处理的问题 ──────────────────────────────────────
# GBA 在 44100Hz、59.7275fps 下每帧只产生 44100/59.7275 ≈ 738 个样本。
# 如果每帧都调用 write() 而 frames_per_buffer = 1024，那么
#     每帧播放 1024 样本 = 23.2ms
#     每帧只产生 738 样本 = 16.7ms
# 缺口逐帧累积 → AO 被"喂不饱"（underrun）→ 声音断续；
# 同时固件侧 blip 缓冲会积压（gba/audio.c 在 avail 超过 audio->samples
# 时会停止写入新样本），声音会失真。
#
# ⇒ 正确做法：**攒够一个整块再写，且只写整块**。
#    本模块在 Python 侧维护累积缓冲，累积到 >= frames_per_buffer 才写一次，
#    余数留到下一帧，从而让"写入速率"精确等于"产生速率"。
# ==================================================================
import time

LOG_PATH = "/sdcard/gba_boot.log"

# 每次送往 AO 的块大小（样本数）。1024 样本 ≈ 23.2ms，
# 既不至于太长的延迟，又能容纳约 1.4 个 GBA 帧的产生量。
DEFAULT_FRAMES_PER_BUFFER = 1024

# 先收集约70ms的原始PCM，再启用AO并提交；保留余量吸收帧间抖动。
# 没有插入静音，也没有改变采样率或模拟速度。
PREBUFFER_BLOCKS = 3

# 累积缓冲的上限（字节）。若因为模拟器跑不到满速而持续积压，
# 就丢弃最旧的数据而不是无限增长 —— 宁可丢一点也不要吃光内存。
MAX_ACCUM_BYTES = DEFAULT_FRAMES_PER_BUFFER * 2 * 2 * 8


def _log(msg):
    print("[gba] audio: %s" % msg)
    try:
        f = open(LOG_PATH, "a")
        f.write("[gba] audio: %s\n" % msg)
        f.close()
    except Exception:
        pass


class GBAudio:
    """GBA 音频输出。init() 返回 False 表示静音降级，不影响游戏。"""

    def __init__(self, rate=44100, frames_per_buffer=DEFAULT_FRAMES_PER_BUFFER):
        self.rate = rate
        self.frames_per_buffer = frames_per_buffer
        # 每次写入必须恰好这么多字节（分块对齐）
        self.block_bytes = frames_per_buffer * 2 * 2
        self.p = None
        self.stream = None
        self._accum = bytearray()
        self._warned = 0
        self.blocks_written = 0
        self.blocks_failed = 0
        self.last_write_rc = None
        self.pcm_nonzero_chunks = 0
        self.pcm_zero_chunks = 0
        self.blocks_dropped = 0
        self._started = False
        self._last_send_us = None
        self.max_send_gap_us = 0
        self.max_write_us = 0
        self.volume_read = None
        self.volume_level = 80

    # ---------------- 初始化 ----------------
    def init(self):
        try:
            # 与官方例程一致：media.pyaudio 是冻结进固件的包
            # （boards/manifest.py: package("media", base_path="../builtin_py")），
            # 导出 PyAudio / paInt16 / LEFT / RIGHT 等名字。
            from media.pyaudio import PyAudio, paInt16
        except Exception as e:
            _log("导入 media.pyaudio 失败: %r" % e)
            return False

        try:
            self.p = PyAudio()
            # 官方例程会调用 initialize(CHUNK)；厂商版 pyaudio.py 里没有这个方法，
            # 因此"有则调用"，避免版本差异直接抛异常。
            init_fn = getattr(self.p, "initialize", None)
            if init_fn is not None:
                init_fn(self.frames_per_buffer)

            self.stream = self.p.open(
                format=paInt16,
                channels=2,
                rate=self.rate,
                output=True,
                frames_per_buffer=self.frames_per_buffer,
                start=False,
            )

            _log("PyAudio 已准备，收集3整块后启用播放 rate=%d block=%d 样本(%d 字节)"
                 % (self.rate, self.frames_per_buffer, self.block_bytes))
            return True
        except Exception as e:
            _log("PyAudio 初始化失败: %r" % e)
            self.deinit()
            return False

    # ---------------- 每帧喂数据 ----------------
    def pump(self, gba):
        """把本帧新产生的 PCM 收进累积缓冲，够一块就写出去。

        返回本次实际写出的字节数（0 = 还没攒够 / 无数据）。
        """
        if self.stream is None:
            return 0

        # 本帧新数据（可能为空 bytes）
        try:
            if gba.audio_avail() > 0:
                data = gba.audio_frame()
                if isinstance(data, (bytes, bytearray)) and data:
                    if any(data):
                        self.pcm_nonzero_chunks += 1
                    else:
                        self.pcm_zero_chunks += 1
                    self._accum.extend(data)
        except Exception as e:
            if self._warned < 3:
                _log("audio_frame 失败: %r" % e)
                self._warned += 1

        # 积压过多就丢最旧的（模拟器跑不到满速时的保护）
        if len(self._accum) > MAX_ACCUM_BYTES:
            drop = len(self._accum) - self.block_bytes
            drop -= drop % 4          # 保持 4 字节（一个立体声样本）对齐
            if drop > 0:
                # MicroPython 的 bytearray 不支持切片删除（del a[:n] 会抛
                # "doesn't support item deletion"），所以用切片重建。
                self._accum = self._accum[drop:]
                self.blocks_dropped += 1

        # 先收集足够播放余量。启动后的自然送音间隔会在1/2个GBA帧
        # (约16/33ms)间交替，一块1024样本只覆盖23ms。
        if not self._started:
            if len(self._accum) < PREBUFFER_BLOCKS * self.block_bytes:
                return 0
            self.stream.start_stream()
            self._started = True
            try:
                self.stream.volume(vol=self.volume_level)
                self.volume_read = self.stream.volume()
            except Exception:
                pass
            # 不在此落盘日志，以免首批PCM尚未入队就停顿。

        # 攒够整块才写：write() 实际发声长度固定为 block_bytes，
        # 所以**绝不能**送不足一块的数据，否则会被补零、
        # 播放时间仍然按整块计算 → underrun。
        written = 0
        while len(self._accum) >= self.block_bytes:
            chunk = bytes(self._accum[:self.block_bytes])
            # 同上：不能用 del，用切片重建
            self._accum = self._accum[self.block_bytes:]
            try:
                before_us = time.ticks_us()
                rc = self.stream.write(chunk)
                after_us = time.ticks_us()
                elapsed_us = time.ticks_diff(after_us, before_us)
                if elapsed_us > self.max_write_us:
                    self.max_write_us = elapsed_us
                self.last_write_rc = rc
                # Vendor PyAudio returns the AO send status (0 on success);
                # some firmware variants return None after a successful send.
                if rc is None or rc == 0:
                    written += len(chunk)
                    self.blocks_written += 1
                    if self._last_send_us is not None:
                        gap_us = time.ticks_diff(after_us, self._last_send_us)
                        if gap_us > self.max_send_gap_us:
                            self.max_send_gap_us = gap_us
                    self._last_send_us = after_us
                else:
                    self.blocks_failed += 1
                    if self._warned < 3:
                        _log("write 返回 AO 错误码: %r" % (rc,))
                        self._warned += 1
            except Exception as e:
                self.blocks_failed += 1
                if self._warned < 3:
                    _log("write 失败: %r" % e)
                    self._warned += 1
                break
        return written

    def set_volume(self, value):
        """设置0..100音量，不重启AO流；返回是否接受。"""
        try:
            value = int(value)
        except Exception:
            return False
        value = max(0, min(100, value))
        if self.stream is None:
            return False
        if value == self.volume_level:
            return True
        if not self._started:
            self.volume_level = value
            return True
        try:
            if self.stream.volume(vol=value) is False:
                return False
        except Exception:
            return False
        self.volume_level = value
        try:
            self.volume_read = self.stream.volume()
        except Exception:
            pass
        return True

    # ---------------- 释放 ----------------
    def deinit(self):
        try:
            if self.stream is not None:
                try:
                    self.stream.stop_stream()
                except Exception:
                    pass
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
        except Exception:
            pass
        try:
            if self.p is not None:
                self.p.terminate()
                self.p = None
        except Exception:
            pass
        self._accum = bytearray()
        self._started = False
        self._last_send_us = None

    # ---------------- 诊断 ----------------
    def stats(self):
        return {
            "rate": self.rate,
            "block_samples": self.frames_per_buffer,
            "blocks_written": self.blocks_written,
            "blocks_failed": self.blocks_failed,
            "blocks_dropped": self.blocks_dropped,
            "last_write_rc": self.last_write_rc,
            "pcm_nonzero_chunks": self.pcm_nonzero_chunks,
            "pcm_zero_chunks": self.pcm_zero_chunks,
            "pending_bytes": len(self._accum),
            "started": self._started,
            "prebuffer_blocks": PREBUFFER_BLOCKS,
            "max_send_gap_us": self.max_send_gap_us,
            "max_write_us": self.max_write_us,
            "volume_read": self.volume_read,
            "volume_level": self.volume_level,
        }
