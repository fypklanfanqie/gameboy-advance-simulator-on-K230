#!/usr/bin/env python3
"""Host tests for headphone channels, standalone silence and boot hooks.

No hardware, card, ROM, save or real log file is opened by these tests.
"""

import ast
import io
import math
import struct
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import audio_tone_probe as probe

checks = []
failures = []


def check(condition, description):
    checks.append(description)
    print("  %-72s %s" % (description, "PASS" if condition else "FAIL"))
    if not condition:
        failures.append(description)


def test_waveform():
    blocks = probe.make_tone_blocks()
    check(len(blocks) == 11 and all(len(block) == 4096 for block in blocks) and
          sum(map(len, blocks)) == 45056, "eleven full stereo blocks use exactly 45056 bytes")
    check(probe.PERIOD_SAMPLES == 176 and
          math.gcd(probe.PERIOD_SAMPLES, probe.BLOCK_SAMPLES) == 16 and
          len(set(blocks)) == 11, "all eleven phases are distinct before the phase cycle repeats")
    stereo = list(struct.iter_unpack("<hh", b"".join(blocks)))
    period = [int(probe.AMPLITUDE * math.sin(2 * math.pi * n / 176))
              for n in range(176)]
    check(all(left == right == period[index % 176]
              for index, (left, right) in enumerate(stereo)),
          "every sample matches a continuous sine in both channels")
    samples = [left for left, _right in stereo]
    check(min(samples) == -4096 and max(samples) == 4096 and sum(samples) == 0,
          "complete cycle has exact quiet amplitude and zero DC offset")
    bound = math.ceil(2 * probe.AMPLITUDE * math.sin(math.pi / 176)) + 1
    check(all(abs(samples[(n + 1) % len(samples)] - value) <= bound
              for n, value in enumerate(samples)),
          "sample increments remain bounded including every block and cycle boundary")
    check(all(struct.unpack_from("<h", block)[0] == period[(n * 1024) % 176]
              for n, block in enumerate(blocks)),
          "each block starts at the correct progressing phase")


def test_channel_waveforms():
    period = [int(4096 * math.sin(2 * math.pi * n / 176)) for n in range(176)]
    expected_channels = {"left": lambda sample: (sample, 0),
                         "right": lambda sample: (0, sample),
                         "same": lambda sample: (sample, sample),
                         "opposite": lambda sample: (sample, -sample)}
    for mode, channels in expected_channels.items():
        blocks = probe.make_tone_blocks(channel_mode=mode)
        decoded = list(struct.iter_unpack("<hh", b"".join(blocks)))
        check(len(blocks) == 11 and all(isinstance(block, bytes) and len(block) == 4096
                                      for block in blocks),
              "%s channel waveform uses eleven full immutable stereo blocks" % mode)
        check(all(pair == channels(period[index % 176])
                  for index, pair in enumerate(decoded)),
              "%s channel PCM matches the requested channel relationship exactly" % mode)
        check(all(sum(pair[side] for pair in decoded) == 0 for side in (0, 1)) and
              all(-4096 <= value <= 4096 for pair in decoded for value in pair),
              "%s channel waveform has zero DC and bounded sample amplitude" % mode)
    try:
        probe.make_tone_blocks(channel_mode="invalid")
    except ValueError:
        check(True, "unknown channel mode is rejected before playback")
    else:
        check(False, "unknown channel mode is rejected before playback")


def run_fake(seconds=0.3, failure=None, write_rc=0, audible=None, channels=False,
             write_failure_at=None):
    events, writes, logs, prints = [], [], [], []

    class Clock:
        us = 0

        def ticks_us(self):
            return self.us

        @staticmethod
        def ticks_diff(a, b):
            return a - b

        def sleep_ms(self, milliseconds):
            event("drain")
            self.us += milliseconds * 1000

    clock = Clock()

    def event(name):
        events.append(name)
        if failure == name:
            raise OSError(name + " failed")

    class Stream:
        def volume(self, vol=None):
            event("volume set" if vol is not None else "volume read")
            if vol is not None:
                assert vol == 80
                return 0
            return (80, 80)

        def write(self, data):
            event("write")
            if write_failure_at == len(writes):
                raise OSError("indexed write failed")
            writes.append(data)
            clock.us += 23220
            if failure == "write interrupt":
                raise KeyboardInterrupt
            return write_rc(len(writes) - 1, data) if callable(write_rc) else write_rc

        def stop_stream(self):
            event("stop_stream")

        def close(self):
            event("close")

    class Manager:
        def __init__(self):
            event("construct")

        def initialize(self, chunk):
            assert chunk == 1024
            event("initialize")

        def open(self, **kwargs):
            assert kwargs == dict(format=1, channels=2, rate=44100, output=True,
                                  frames_per_buffer=1024)
            event("open")
            return Stream()

        def terminate(self):
            event("terminate")

    class LogFile(io.StringIO):
        def close(self):
            logs.append(self.getvalue())
            super().close()

    def fake_open(path, mode):
        assert path == "/sdcard/gba_boot.log" and mode == "a"
        event("log")
        return LogFile()

    def fake_print(value):
        events.append("print")
        prints.append(value)

    original_tone_factory = probe.make_tone_blocks

    def make_tones(*args, **kwargs):
        events.append("tone blocks")
        return original_tone_factory(*args, **kwargs)

    media = types.ModuleType("media")
    media_media = types.ModuleType("media.media")
    media_media.MediaManager = types.SimpleNamespace(
        init=lambda: event("media init"), deinit=lambda: event("media deinit"))
    media_pyaudio = types.ModuleType("media.pyaudio")
    media_pyaudio.PyAudio, media_pyaudio.paInt16 = Manager, 1
    result, interrupted = None, False
    with patch.dict(sys.modules, {"media": media, "media.media": media_media,
                                  "media.pyaudio": media_pyaudio}), \
            patch.object(probe, "time", clock), \
            patch.object(probe, "make_tone_blocks", make_tones), \
            patch.object(probe, "open", fake_open, create=True), \
            patch.object(probe, "print", fake_print, create=True):
        try:
            if channels:
                result = probe.run_channels(seconds_per_stage=seconds)
            else:
                result = (probe.run(seconds) if audible is None else
                          probe.run(seconds, audible=audible))
        except KeyboardInterrupt:
            interrupted = True
    return result, events, writes, logs, prints, interrupted


def test_run():
    result, events, writes, logs, prints, _interrupt = run_fake()
    requested = math.ceil(0.3 * 44100 / 1024)
    tones = probe.make_tone_blocks()
    check(result["mode"] == "tone" and result["written"] == requested and result["failed"] == 0 and
          writes == [tones[n % 11] for n in range(requested)],
          "run sends the complete eleven-phase sequence and wraps correctly")
    check(result["rate"] == 44100 and result["period_samples"] == 176 and
          result["phase_blocks"] == 11 and result["tone_range"] == (-4096, 4096) and
          result["volume_requested"] == 80 and result["volume_read"] == (80, 80),
          "summary records rate, period, phase count, tone range and volume")
    check(result["elapsed_us"] == requested * 23220 and
          abs(result["submit_samples_per_s"] - 44100) < 2,
          "submit rate excludes the final AO drain wait")
    check(events[-6:] == ["stop_stream", "close", "terminate",
                          "media deinit", "log", "print"] and
          len(logs) == len(prints) == 1 and logs[0] == prints[0] + "\n",
          "one summary appends and prints only after all cleanup")
    result, events, writes, logs, prints, _interrupt = run_fake(write_rc=-1)
    check(result["written"] == 0 and result["failed"] == 1 and
          result["last_rc"] == -1 and len(writes) == 1,
          "AO error status stops immediately and never counts a successful block")
    result, *_rest = run_fake(write_rc=None)
    check(result["written"] == requested and result["failed"] == 0,
          "compatible streams returning None still report successful submissions")
    for failure in ("media init", "construct", "initialize", "open",
                    "volume set", "volume read", "write"):
        result, events, _writes, logs, prints, _interrupt = run_fake(failure=failure)
        expected = ["media deinit", "log", "print"]
        if failure not in ("media init", "construct"):
            expected.insert(0, "terminate")
        if failure in ("volume set", "volume read", "write"):
            expected = ["stop_stream", "close"] + expected
        check(result["error"] is not None and result["written"] == 0 and
              result["failed"] == (1 if failure == "write" else 0) and
              events[-len(expected):] == expected and len(logs) == len(prints) == 1,
              "%s failure releases all acquired resources and reports once" % failure)
    for failure in ("stop_stream", "close", "terminate", "media deinit"):
        result, events, _writes, _logs, prints, _interrupt = run_fake(failure=failure)
        check(len(result["cleanup_errors"]) == 1 and
              events[-6:] == ["stop_stream", "close", "terminate",
                              "media deinit", "log", "print"] and len(prints) == 1,
              "%s error does not prevent later cleanup or final summary" % failure)
    result, events, _writes, _logs, prints, interrupted = run_fake(failure="write interrupt")
    check(interrupted and result is None and events[-6:] ==
          ["stop_stream", "close", "terminate", "media deinit", "log", "print"] and
          "KeyboardInterrupt" in prints[0],
          "KeyboardInterrupt propagates only after cleanup and diagnostic summary")
    result, events, _writes, logs, prints, _interrupt = run_fake(failure="log")
    check(not logs and len(prints) == 1 and "log_error" in result,
          "log failure still prints the summary after releasing audio resources")
    for seconds in (0, -1):
        try:
            probe.run(seconds)
        except ValueError:
            check(True, "nonpositive duration is rejected before importing board APIs")
        else:
            check(False, "nonpositive duration is rejected before importing board APIs")


def test_silence():
    requested = math.ceil(0.3 * 44100 / 1024)
    result, events, writes, logs, prints, _interrupt = run_fake(audible=False)
    check(result["mode"] == "silence" and result["written"] == requested and
          result["failed"] == 0 and len(writes) == requested and
          all(isinstance(block, bytes) and len(block) == 4096 and not any(block)
              for block in writes),
          "standalone silence submits exactly zero full stereo blocks")
    check("tone blocks" not in events, "standalone silence never constructs sine blocks")
    check(result["rate"] == 44100 and result["block_samples"] == 1024 and
          result["frequency_hz"] == result["period_samples"] == 0 and
          result["phase_blocks"] == 1 and result["tone_range"] == (0, 0) and
          result["volume_requested"] == 80 and result["volume_set"] == 0 and
          result["volume_read"] == (80, 80),
          "silent diagnostics retain output parameters and report zero tone")
    check(events[:5] == ["media init", "construct", "initialize", "open", "volume set"] and
          events[-7:] == ["drain", "stop_stream", "close", "terminate",
                          "media deinit", "log", "print"] and
          len(logs) == len(prints) == 1 and result["elapsed_us"] == requested * 23220,
          "silence uses the same AO lifecycle and summarizes only after cleanup")
    result, _events, writes, _logs, _prints, _interrupt = run_fake(audible=False, write_rc=-1)
    check(result["written"] == 0 and result["failed"] == 1 and len(writes) == 1,
          "silent AO rejection stops immediately and counts the failure")
    for failure in ("media init", "construct", "initialize", "open",
                    "volume set", "volume read", "write"):
        result, events, _writes, logs, prints, _interrupt = run_fake(audible=False, failure=failure)
        expected = ["media deinit", "log", "print"]
        if failure not in ("media init", "construct"):
            expected.insert(0, "terminate")
        if failure in ("volume set", "volume read", "write"):
            expected = ["stop_stream", "close"] + expected
        check(result["error"] is not None and result["mode"] == "silence" and
              result["written"] == 0 and events[-len(expected):] == expected and
              "tone blocks" not in events and len(logs) == len(prints) == 1,
              "silent %s failure cleans up and reports once" % failure)
    for failure in ("drain", "stop_stream", "close", "terminate", "media deinit"):
        result, events, _writes, _logs, prints, _interrupt = run_fake(audible=False, failure=failure)
        check(len(result["cleanup_errors"]) == 1 and
              events[-6:] == ["stop_stream", "close", "terminate",
                              "media deinit", "log", "print"] and len(prints) == 1,
              "silent %s error still completes remaining cleanup" % failure)


def test_channel_run():
    seconds = 0.05
    result, events, writes, logs, prints, _interrupt = run_fake(
        seconds=seconds, channels=True)
    count = math.ceil(seconds * 44100 / 1024)
    gap = math.ceil(0.5 * 44100 / 1024)
    total = 4 * count + 3 * gap
    check(result["mode"] == "channels" and result["seconds_per_stage"] == seconds and
          result["gap_seconds"] == 0.5 and result["gap_blocks"] == gap and
          result["ramp_samples"] == 220 and result["blocks_requested"] == total and
          result["written"] == total and result["failed"] == 0 and
          len(writes) == total and result["separator_written"] == 3 * gap,
          "four channel stages submit the expected blocks and separators")
    check(events.count("open") == events.count("stop_stream") ==
          events.count("close") == 1 and events.count("construct") ==
          events.count("terminate") == 1 and events.count("media init") ==
          events.count("media deinit") == 1,
          "all four stages share one AO stream lifecycle")
    tones = {mode: probe.make_tone_blocks(channel_mode=mode)
             for mode in ("left", "right", "same", "opposite")}
    modes = ("left", "right", "same", "opposite")
    valid = True
    for stage_index, mode in enumerate(modes):
        start = stage_index * (count + gap)
        stage_writes = writes[start:start + count]
        gap_writes = writes[start + count:start + count + gap]
        valid = valid and all(not any(block) for block in gap_writes)
        valid = valid and not any(stage_writes[0][:4])
        valid = valid and not any(stage_writes[-1][-4:])
        valid = valid and stage_writes[0][220 * 4:] == tones[mode][0][220 * 4:]
        valid = valid and stage_writes[-1][:-220 * 4] == tones[mode][(count - 1) % 11][:-220 * 4]
        if count > 2:
            valid = valid and stage_writes[1] == tones[mode][1]
    check(valid, "stage order, exact zero gaps and five-millisecond boundary ramps are correct")
    check([stage["name"] for stage in result["stages"]] == list(modes) and
          all(stage["blocks_requested"] == count and stage["written"] == count and
              stage["failed"] == 0 for stage in result["stages"]) and
          len(logs) == len(prints) == 1 and
          events[-7:] == ["drain", "stop_stream", "close", "terminate",
                          "media deinit", "log", "print"],
          "stage statistics and final summary are emitted after shared cleanup")
    result, events, writes, logs, prints, _interrupt = run_fake(
        seconds=seconds, channels=True, write_failure_at=count + gap)
    check(result["error"] is not None and result["written"] == count + gap and
          result["failed"] == 1 and result["stages"][1]["failed"] == 1 and
          events.count("open") == events.count("close") == 1 and
          events[-6:] == ["stop_stream", "close", "terminate",
                          "media deinit", "log", "print"],
          "mid-sequence channel write failure records stage error and releases one stream")
    for failure in ("media init", "construct", "initialize", "open",
                    "volume set", "volume read", "write"):
        result, events, _writes, logs, prints, _interrupt = run_fake(
            seconds=seconds, channels=True, failure=failure)
        expected = ["media deinit", "log", "print"]
        if failure not in ("media init", "construct"):
            expected.insert(0, "terminate")
        if failure in ("volume set", "volume read", "write"):
            expected = ["stop_stream", "close"] + expected
        check(result["error"] is not None and result["written"] == 0 and
              result["failed"] == (1 if failure == "write" else 0) and
              events[-len(expected):] == expected and len(logs) == len(prints) == 1,
              "channel %s failure releases resources and reports once" % failure)
    for failure in ("drain", "stop_stream", "close", "terminate", "media deinit"):
        result, events, _writes, _logs, prints, _interrupt = run_fake(
            seconds=seconds, channels=True, failure=failure)
        check(len(result["cleanup_errors"]) == 1 and result["written"] == total and
              events[-6:] == ["stop_stream", "close", "terminate",
                              "media deinit", "log", "print"] and len(prints) == 1,
              "channel %s failure allows all remaining cleanup" % failure)
    for failed_index in (1, count, count + gap, 3 * (count + gap)):
        result, events, writes, _logs, _prints, _interrupt = run_fake(
            seconds=seconds, channels=True,
            write_rc=lambda index, _data: -1 if index == failed_index else 0)
        check(result["written"] == failed_index and result["failed"] == 1 and
              result["last_rc"] == -1 and len(writes) == failed_index + 1 and
              result["error"] is not None and events.count("close") == 1,
              "channel status failure at block %d stops further writes and cleans up" % failed_index)
    result, _events, _writes, _logs, _prints, _interrupt = run_fake(
        seconds=seconds, channels=True, write_rc=None)
    check(result["written"] == total and result["failed"] == 0,
          "channel diagnostic accepts vendor-compatible None write status")
    result, events, _writes, _logs, prints, interrupted = run_fake(
        seconds=seconds, channels=True, failure="write interrupt")
    check(interrupted and result is None and events[-6:] ==
          ["stop_stream", "close", "terminate", "media deinit", "log", "print"] and
          "KeyboardInterrupt" in prints[0],
          "channel interruption propagates after cleanup and one diagnostic summary")
    result, _events, _writes, logs, prints, _interrupt = run_fake(
        seconds=seconds, channels=True, failure="log")
    check(not logs and len(prints) == 1 and "log_error" in result,
          "channel log failure still prints final summary")
    for seconds_invalid in (0, -1):
        try:
            probe.run_channels(seconds_per_stage=seconds_invalid)
        except ValueError:
            check(True, "nonpositive stage duration is rejected before board imports")
        else:
            check(False, "nonpositive stage duration is rejected before board imports")


def test_boot_hook():
    tree = ast.parse((ROOT / "src" / "main.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "run_audio_probe_once")
    extracted = ast.Module(body=[function], type_ignores=[])

    def scenario(present, delete_failure=False, probe_failure=False, repeat=False,
                 reported_failure=None):
        events, errors = [], []
        state = {"present": present}

        def stat(path):
            events.append("stat")
            assert path == "/sdcard/audio_probe.once"
            if not state["present"]:
                raise OSError("missing")
            return ()

        def remove(path):
            events.append("remove")
            assert path == "/sdcard/audio_probe.once"
            if delete_failure:
                raise OSError("read only")
            state["present"] = False

        def run_channels(seconds_per_stage):
            events.append(("channels", seconds_per_stage))
            if probe_failure:
                raise OSError("AO unavailable")
            result = {"error": None, "failed": 0, "cleanup_errors": []}
            if reported_failure:
                result[reported_failure] = (["cleanup error"] if
                                            reported_failure == "cleanup_errors" else 1)
            return result

        fake_probe = types.ModuleType("audio_tone_probe")
        fake_probe.run_channels = run_channels
        namespace = {"os": types.SimpleNamespace(stat=stat, remove=remove),
                     "AUDIO_PROBE_ONCE_PATH": "/sdcard/audio_probe.once",
                     "log": lambda _msg: events.append("log"),
                     "log_exception": lambda tag, exc: errors.append((tag, repr(exc)))}
        exec(compile(extracted, "boot_probe_hook.py", "exec"), namespace)
        returns = []
        with patch.dict(sys.modules, {"audio_tone_probe": fake_probe}):
            returns.append(namespace["run_audio_probe_once"]())
            if repeat:
                returns.append(namespace["run_audio_probe_once"]())
        return events, errors, state, returns

    events, errors, _state, returns = scenario(False)
    check(events == ["stat"] and not errors and returns == [False],
          "absent marker returns False without running the probe")
    events, errors, state, returns = scenario(True, repeat=True)
    check(events == ["stat", "remove", "log", ("channels", 3), "log", "stat"] and
          not errors and not state["present"] and returns == [True, False],
          "boot consumes marker and returns True only for its single probe")
    events, errors, state, returns = scenario(True, probe_failure=True, repeat=True)
    check(events == ["stat", "remove", "log", ("channels", 3), "stat"] and
          len(errors) == 1 and not state["present"] and returns == [False, False],
          "probe exception is reported without escaping or repeating next boot")
    events, errors, state, returns = scenario(True, delete_failure=True)
    check(events == ["stat", "remove"] and len(errors) == 1 and state["present"] and
          returns == [False],
          "failed marker deletion prevents starting audio and reports the error")
    for failure in ("error", "failed", "cleanup_errors"):
        events, errors, state, returns = scenario(True, reported_failure=failure, repeat=True)
        check(events == ["stat", "remove", "log", ("channels", 3), "log", "stat"] and
              not errors and not state["present"] and returns == [False, False],
              "reported probe %s returns False without repeating the marker" % failure)


def test_boot_sequence():
    tree = ast.parse((ROOT / "src" / "main.py").read_text(encoding="utf-8"))
    names = {"run_audio_probe_once", "main",
             "run_emulator", "_run_emulator"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
                 node.name in names]
    assert {node.name for node in functions} == names
    check(not any(isinstance(node, ast.FunctionDef) and node.name == "run_audio_warmup"
                  for node in tree.body), "production boot has no separate warmup lifecycle")
    extracted = ast.Module(body=functions, type_ignores=[])

    def scenario(marker=False, probe_error=None, audio=True, boot_mode="emulator",
                 missing_core=False, delete_failure=False, repeat=False):
        events, logs, errors = [], [], []
        state = {"marker": marker}

        def stat(path):
            assert path == "/sdcard/audio_probe.once"
            events.append("marker stat")
            if not state["marker"]:
                raise OSError("missing")
            return ()

        def remove(path):
            assert path == "/sdcard/audio_probe.once"
            events.append("marker remove")
            if delete_failure:
                raise OSError("read only")
            state["marker"] = False

        def run_channels(seconds_per_stage):
            events.append(("channels", seconds_per_stage))
            if probe_error == "exception":
                raise OSError("audio unavailable")
            result = {"error": None, "failed": 0, "cleanup_errors": []}
            if probe_error:
                result[probe_error] = ["cleanup failed"] if probe_error == "cleanup_errors" else 1
            events.append("channels released")
            return result

        class Input:
            touch_async = True

            def __init__(self):
                events.append("input init")

            def deinit(self):
                events.append("input deinit")

        class Core:
            def load(self, path):
                assert path == "/mock/test.gba"
                events.append("fake load")
                return 0

            def save_type(self):
                return 3

            def save_size(self):
                return 131072

            def save_load(self, path):
                assert path == "/mock/test.sav"
                events.append("fake save load")
                return 131072

            def unload(self):
                events.append("unload")

        def init_display(*args, **kwargs):
            events.append("display init")
            # Stop the actual emulator before frame loop; no display or save exists.
            return False

        fake_probe = types.ModuleType("audio_tone_probe")
        fake_probe.run_channels = run_channels
        fake_input = types.ModuleType("gba_input")
        fake_input.GBAInput = Input
        namespace = {"os": types.SimpleNamespace(stat=stat, remove=remove,
                     EXITPOINT_ENABLE=1, exitpoint=lambda _n: events.append("exitpoint")),
                     "time": types.SimpleNamespace(ticks_ms=lambda: 0),
                     "AUDIO_PROBE_ONCE_PATH": "/sdcard/audio_probe.once",
                     "AUDIO_ENABLE": audio,
                     "BOOT_MODE": boot_mode, "_DISPLAY_ERR": None, "_UI_ERR": None,
                     "log": logs.append,
                     "log_exception": lambda tag, exc: errors.append((tag, repr(exc))),
                     "firmware_identity": lambda: None if missing_core else Core(),
                     "show_message": lambda *args: events.append("message"),
                     "selftest": lambda: events.append("selftest"),
                     "ROM_PATH": "/mock/test.gba", "SAVE_PATH": "/mock/test.sav",
                     "_save_exists": lambda path: path == "/mock/test.sav",
                     "layout_params": lambda: (3, 0, 0, 40, 0),
                     "init_display": init_display, "SHOW_CONTROLS": True}
        exec(compile(extracted, "actual_boot_functions.py", "exec"), namespace)
        with patch.dict(sys.modules, {"audio_tone_probe": fake_probe, "gba_input": fake_input}):
            namespace["main"]()
            if repeat:
                namespace["main"]()
        return events, logs, errors, state

    events, _logs, errors, _state = scenario()
    check(events == ["exitpoint", "marker stat", "input init", "fake load",
                     "fake save load", "display init", "unload", "input deinit"] and
          not errors, "ordinary boot enters the game without any diagnostic audio lifecycle")
    events, _logs, errors, state = scenario(marker=True, repeat=True)
    check(events.count(("channels", 3)) == 1 and events.count("input init") == 2 and
          events.index("marker remove") < events.index(("channels", 3)) <
          events.index("channels released") < events.index("input init") <
          events.index("display init") and not errors and not state["marker"],
          "marker boot runs one channel test and releases it before input and display")
    for issue in ("error", "failed", "cleanup_errors", "exception"):
        events, logs, errors, state = scenario(marker=True, probe_error=issue, repeat=True)
        check(events.count(("channels", 3)) == 1 and events.count("input init") == 2 and
              events.index(("channels", 3)) < events.index("input init") <
              events.index("display init") and not state["marker"] and
              (bool(errors) if issue == "exception" else
               any("耳机测试报告异常" in line for line in logs)),
              "channel probe %s is reported and never repeats before continuing game" % issue)
    events, _logs, errors, state = scenario(marker=True, delete_failure=True)
    check(("channels", 3) not in events and
          events.index("marker remove") < events.index("input init") <
          events.index("display init") and len(errors) == 1 and state["marker"],
          "marker deletion failure avoids audio and still initializes the game")
    events, _logs, errors, state = scenario(marker=True, audio=False)
    check("marker stat" not in events and ("channels", 3) not in events and
          "display init" in events and state["marker"] and not errors,
          "disabled audio neither consumes marker nor creates diagnostic audio")
    for mode in ("none", "selftest"):
        events, _logs, errors, _state = scenario(boot_mode=mode)
        check("marker stat" not in events and ("channels", 3) not in events and
              "input init" not in events and "display init" not in events and not errors,
              "%s boot bypasses all emulator audio and input setup" % mode)
    events, _logs, errors, _state = scenario(missing_core=True)
    check(events == ["exitpoint", "message"] and not errors,
          "missing core returns before audio probe and emulator resources")


def main():
    print("host headphone-probe, channel diagnostics and boot sequence tests")
    test_waveform()
    test_channel_waveforms()
    test_run()
    test_silence()
    test_channel_run()
    test_boot_hook()
    test_boot_sequence()
    print("checks: %d, failures: %d" % (len(checks), len(failures)))
    print("RESULT: %s" % ("FAIL" if failures else "PASS"))
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
