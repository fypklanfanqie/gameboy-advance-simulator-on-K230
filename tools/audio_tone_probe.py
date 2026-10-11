"""K230 headphone probe using the verified vendor PyAudio API.

No ROM or save is opened. Period 176 and block size 1024 advance phase by
144 samples per block; all 11 phases are precomputed in 45056 bytes. Blocks
form one continuous quiet stereo sine, including the wrap from block 10 to 0.
Unlike a same-phase repeating block, erroneous repeated/overwritten blocks
are easier to hear. A clean tone does not prove driver buffer lifetime.
run_channels compares left, right, same-phase and opposite-phase audio using
one stream. Only the final summary is logged/printed after cleanup.
"""

import math
import struct
import time

RATE = 44100
BLOCK_SAMPLES = 1024
PERIOD_SAMPLES = 176
PHASE_BLOCKS = 11  # 176 / gcd(176, 1024), gcd = 16
AMPLITUDE = 4096
VOLUME = 80
LOG_PATH = "/sdcard/gba_boot.log"


CHANNEL_MODES = ("left", "right", "same", "opposite")
RAMP_SAMPLES = 220  # About 5 ms; applied only at diagnostic stage boundaries.


def make_tone_blocks(channel_mode="same"):
    """Generate a complete block-phase cycle for the selected channels."""
    if channel_mode not in CHANNEL_MODES:
        raise ValueError("unknown channel mode: %s" % channel_mode)
    period = tuple(int(AMPLITUDE * math.sin(2 * math.pi * n / PERIOD_SAMPLES))
                   for n in range(PERIOD_SAMPLES))
    blocks = []
    for phase in range(PHASE_BLOCKS):
        data = bytearray(BLOCK_SAMPLES * 4)
        for index in range(BLOCK_SAMPLES):
            sample = period[(phase * BLOCK_SAMPLES + index) % PERIOD_SAMPLES]
            if channel_mode == "left":
                left, right = sample, 0
            elif channel_mode == "right":
                left, right = 0, sample
            elif channel_mode == "opposite":
                left, right = sample, -sample
            else:
                left, right = sample, sample
            struct.pack_into("<hh", data, index * 4, left, right)
        blocks.append(bytes(data))
    return tuple(blocks)


def _summary(stats):
    line = "[audio-tone] %r" % stats
    try:
        with open(LOG_PATH, "a") as log_file:
            log_file.write(line + "\n")
    except Exception as exc:
        stats["log_error"] = repr(exc)
        line = "[audio-tone] %r" % stats
    print(line)


def run(seconds=10, audible=True):
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    from media.media import MediaManager
    from media.pyaudio import PyAudio, paInt16

    # Standalone tone/silence helpers; normal boots do not call this function.
    tones = make_tone_blocks() if audible else (bytes(BLOCK_SAMPLES * 4),)
    blocks = int(math.ceil(seconds * RATE / BLOCK_SAMPLES))
    manager = None
    stream = None
    started = None
    stats = {
        "mode": "tone" if audible else "silence",
        "rate": RATE, "frequency_hz": RATE / PERIOD_SAMPLES if audible else 0,
        "period_samples": PERIOD_SAMPLES if audible else 0, "phase_blocks": len(tones),
        "block_samples": BLOCK_SAMPLES,
        "tone_range": (-AMPLITUDE, AMPLITUDE) if audible else (0, 0),
        "seconds_requested": seconds, "blocks_requested": blocks,
        "written": 0, "failed": 0, "last_rc": None,
        "volume_requested": VOLUME, "volume_set": None, "volume_read": None,
        "elapsed_us": 0, "submit_samples_per_s": 0,
        "error": None, "cleanup_errors": [],
    }
    try:
        MediaManager.init()
        manager = PyAudio()
        initialize = getattr(manager, "initialize", None)
        if initialize is not None:
            initialize(BLOCK_SAMPLES)
        stream = manager.open(
            format=paInt16, channels=2, rate=RATE, output=True,
            frames_per_buffer=BLOCK_SAMPLES,
        )
        stats["volume_set"] = stream.volume(vol=VOLUME)
        stats["volume_read"] = stream.volume()
        started = time.ticks_us()
        for index in range(blocks):
            try:
                stats["last_rc"] = stream.write(tones[index % len(tones)])
            except Exception:
                stats["failed"] += 1
                raise
            if stats["last_rc"] is None or stats["last_rc"] == 0:
                stats["written"] += 1
            else:
                stats["failed"] += 1
                break
    except Exception as exc:
        stats["error"] = repr(exc)
    except KeyboardInterrupt:
        stats["error"] = "KeyboardInterrupt"
        raise
    finally:
        if started is not None:
            stats["elapsed_us"] = time.ticks_diff(time.ticks_us(), started)
            stats["submit_samples_per_s"] = (
                stats["written"] * BLOCK_SAMPLES * 1000000 /
                max(stats["elapsed_us"], 1)
            )
        if stream is not None:
            # Vendor AO queues five frames; drain outside the rate metric.
            if stats["written"]:
                try:
                    time.sleep_ms((5 * BLOCK_SAMPLES * 1000 + RATE - 1) // RATE)
                except Exception as exc:
                    stats["cleanup_errors"].append("drain: %r" % exc)
            for name in ("stop_stream", "close"):
                try:
                    getattr(stream, name)()
                except Exception as exc:
                    stats["cleanup_errors"].append("%s: %r" % (name, exc))
        if manager is not None:
            try:
                manager.terminate()
            except Exception as exc:
                stats["cleanup_errors"].append("terminate: %r" % exc)
        try:
            MediaManager.deinit()
        except Exception as exc:
            stats["cleanup_errors"].append("media deinit: %r" % exc)
        _summary(stats)
    return stats


def _ramp_block(block, fade_in=False, fade_out=False):
    """Precompute quiet stage boundaries without changing channel polarity."""
    data = bytearray(block)
    for index in range(BLOCK_SAMPLES):
        gain = RAMP_SAMPLES - 1
        if fade_in and index < RAMP_SAMPLES:
            gain = min(gain, index)
        if fade_out and index >= BLOCK_SAMPLES - RAMP_SAMPLES:
            gain = min(gain, BLOCK_SAMPLES - 1 - index)
        if gain < RAMP_SAMPLES - 1:
            left, right = struct.unpack_from("<hh", block, index * 4)
            left = int(left * gain / (RAMP_SAMPLES - 1))
            right = int(right * gain / (RAMP_SAMPLES - 1))
            struct.pack_into("<hh", data, index * 4, left, right)
    return bytes(data)


def run_channels(seconds_per_stage=3):
    """Compare four channel modes in one AO stream, with half-second gaps."""
    if seconds_per_stage <= 0:
        raise ValueError("seconds_per_stage must be positive")
    from media.media import MediaManager
    from media.pyaudio import PyAudio, paInt16

    count = int(math.ceil(seconds_per_stage * RATE / BLOCK_SAMPLES))
    gap_blocks = int(math.ceil(0.5 * RATE / BLOCK_SAMPLES))
    silence = bytes(BLOCK_SAMPLES * 4)
    segments = []
    stages = []
    # Precompute every mode and boundary before AO starts. No waveform
    # generation, file logging or stage printing interrupts the live stream.
    for mode in CHANNEL_MODES:
        tones = make_tone_blocks(mode)
        first = _ramp_block(tones[0], fade_in=True, fade_out=count == 1)
        last = first if count == 1 else _ramp_block(
            tones[(count - 1) % len(tones)], fade_out=True)
        segments.append((tones, first, last))
        stages.append({"name": mode, "blocks_requested": count,
                       "written": 0, "failed": 0})
    stats = {
        "mode": "channels", "rate": RATE,
        "frequency_hz": RATE / PERIOD_SAMPLES,
        "period_samples": PERIOD_SAMPLES, "phase_blocks": PHASE_BLOCKS,
        "block_samples": BLOCK_SAMPLES, "tone_range": (-AMPLITUDE, AMPLITUDE),
        "seconds_per_stage": seconds_per_stage, "gap_seconds": 0.5,
        "gap_blocks": gap_blocks, "ramp_samples": RAMP_SAMPLES,
        "stages": stages, "separator_written": 0,
        "blocks_requested": len(stages) * count + (len(stages) - 1) * gap_blocks,
        "written": 0, "failed": 0, "last_rc": None,
        "volume_requested": VOLUME, "volume_set": None, "volume_read": None,
        "elapsed_us": 0, "submit_samples_per_s": 0,
        "error": None, "cleanup_errors": [],
    }
    manager = None
    stream = None
    started = None
    try:
        MediaManager.init()
        manager = PyAudio()
        initialize = getattr(manager, "initialize", None)
        if initialize is not None:
            initialize(BLOCK_SAMPLES)
        stream = manager.open(
            format=paInt16, channels=2, rate=RATE, output=True,
            frames_per_buffer=BLOCK_SAMPLES,
        )
        stats["volume_set"] = stream.volume(vol=VOLUME)
        stats["volume_read"] = stream.volume()
        started = time.ticks_us()
        for stage_index, stage in enumerate(stages):
            tones, first, last = segments[stage_index]
            for index in range(count):
                block = (first if index == 0 else last if index == count - 1
                         else tones[index % len(tones)])
                try:
                    stats["last_rc"] = stream.write(block)
                except Exception:
                    stats["failed"] += 1
                    stage["failed"] += 1
                    raise
                if stats["last_rc"] is not None and stats["last_rc"] != 0:
                    stats["failed"] += 1
                    stage["failed"] += 1
                    raise OSError("AO write returned %r" % stats["last_rc"])
                stats["written"] += 1
                stage["written"] += 1
            if stage_index + 1 < len(stages):
                for _ in range(gap_blocks):
                    try:
                        stats["last_rc"] = stream.write(silence)
                    except Exception:
                        stats["failed"] += 1
                        raise
                    if stats["last_rc"] is not None and stats["last_rc"] != 0:
                        stats["failed"] += 1
                        raise OSError("AO separator returned %r" % stats["last_rc"])
                    stats["written"] += 1
                    stats["separator_written"] += 1
    except Exception as exc:
        stats["error"] = repr(exc)
    except KeyboardInterrupt:
        stats["error"] = "KeyboardInterrupt"
        raise
    finally:
        if started is not None:
            stats["elapsed_us"] = time.ticks_diff(time.ticks_us(), started)
            stats["submit_samples_per_s"] = (
                stats["written"] * BLOCK_SAMPLES * 1000000 /
                max(stats["elapsed_us"], 1)
            )
        if stream is not None:
            # Drain five vendor queue slots before shutting down the stream.
            if stats["written"]:
                try:
                    time.sleep_ms((5 * BLOCK_SAMPLES * 1000 + RATE - 1) // RATE)
                except Exception as exc:
                    stats["cleanup_errors"].append("drain: %r" % exc)
            for name in ("stop_stream", "close"):
                try:
                    getattr(stream, name)()
                except Exception as exc:
                    stats["cleanup_errors"].append("%s: %r" % (name, exc))
        if manager is not None:
            try:
                manager.terminate()
            except Exception as exc:
                stats["cleanup_errors"].append("terminate: %r" % exc)
        try:
            MediaManager.deinit()
        except Exception as exc:
            stats["cleanup_errors"].append("media deinit: %r" % exc)
        _summary(stats)
    return stats


if __name__ == "__main__":
    run_channels()
