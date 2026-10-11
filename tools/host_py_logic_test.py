#!/usr/bin/env python3
"""Host-side logic tests for the board Python modules, using fakes.

WHY: the C host test (tools/hosttest) validates the mGBA core, but it cannot
exercise the Python layer that runs on the board - the audio block
accumulation, the save-if-idle guard, the ADC error sentinel and the button /
touch-region mapping. Those are pure logic and CAN be tested off-board by
stubbing the MicroPython-only modules (machine, media.display, image, ...).

This catches real mistakes, e.g.:
  * writing a partial audio block (the board's PyAudio zero-pads it to a full
    block but still plays the full block -> underrun),
  * forgetting to skip a save while the emulated flash is busy,
  * treating the ADC error sentinel (65535) as a joystick deflection.

Run: python tools/host_py_logic_test.py
"""

import io
import gc
import re
import sys
import types
import weakref
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# The board modules live in src/ and import each other by bare name
# (e.g. main.py does `import gba_input`), so src/ must be importable.
sys.path.insert(0, str(SRC))

checks = []
failures = []


def check(cond, what):
    checks.append(what)
    print("  %-64s %s" % (what, "PASS" if cond else "FAIL"))
    if not cond:
        failures.append(what)


def section(name):
    print("\n=== %s ===" % name)


# --------------------------------------------------------------------------
# Stub the MicroPython-only modules BEFORE importing the board modules.
# --------------------------------------------------------------------------
class FakeADC:
    """Mirrors machine_adc_read_u16: drv_adc_read(channel) & 0xFFFF, and
    drv_adc_read returns __UINT32_MAX__ on failure => 65535."""

    def __init__(self, channel, value):
        self.channel = channel
        self.value = value

    def read_u16(self):
        return self.value


class FakeTouchPoint:
    """Mirrors machine_touch_info_type: attribute access, not a tuple."""

    def __init__(self, x, y, event):
        self.x = x
        self.y = y
        self.event = event


class FakeTouch:
    def __init__(self, index=0, points=None):
        self.index = index
        self._points = points or []

    def read(self, n=1):
        return tuple(self._points)


def install_stubs(adc_values=None, touch_points=None):
    """Install fake `machine` (and friends) so the board modules import."""
    machine = types.ModuleType("machine")
    adc_values = adc_values or {0: 2048, 1: 2048}

    def ADC(channel):
        return FakeADC(channel, adc_values.get(channel, 2048))

    def TOUCH(index=0):
        return FakeTouch(index, touch_points)

    machine.ADC = ADC
    machine.TOUCH = TOUCH
    machine.Pin = object
    machine.FPIOA = object
    sys.modules["machine"] = machine


# --------------------------------------------------------------------------
# 1. gba_input: ADC sentinel and direction mapping
# --------------------------------------------------------------------------
def test_input():
    section("1. gba_input: ADC error sentinel and direction mapping")
    for mod in list(sys.modules):
        if mod in ("gba_input",):
            del sys.modules[mod]

    # ADC_MAX is 4095 (12-bit). Centre is ~2048.
    cases = [
        # (x, y, expected mask bits, description)
        (2048, 2048, 0, "centred => no direction"),
        (100, 2048, "LEFT", "x low => LEFT"),
        (4000, 2048, "RIGHT", "x high => RIGHT"),
        (2048, 100, "DOWN", "y low => DOWN"),
        (2048, 4000, "UP", "y high => UP"),
        (0xFFFF, 2048, 0, "x ADC error (65535) => no direction, NOT RIGHT"),
        (2048, 0xFFFF, 0, "y ADC error (65535) => no direction, NOT UP"),
        (0xFFFF, 0xFFFF, 0, "both ADC error => no direction"),
    ]
    expects = {
        "LEFT": 1 << 5, "RIGHT": 1 << 4, "UP": 1 << 6, "DOWN": 1 << 7,
    }
    for x, y, want, desc in cases:
        install_stubs(adc_values={0: x, 1: y}, touch_points=[])
        for m in ("gba_input",):
            sys.modules.pop(m, None)
        import importlib
        import gba_input
        importlib.reload(gba_input)
        got = gba_input.GBAInput().read_joystick()
        want_mask = expects[want] if isinstance(want, str) else want
        check(got == want_mask,
              "%s (mask 0x%03X, want 0x%03X)" % (desc, got, want_mask))


# --------------------------------------------------------------------------
# 2. gba_input: touch mapping uses attributes and ignores EVENT_UP
# --------------------------------------------------------------------------
def test_touch():
    section("2. gba_input: touch uses .x/.y/.event and ignores release")
    import gba_input
    import gba_ui

    # Regions come from the SHARED module, so this also proves main.py and
    # gba_input.py cannot disagree about where the buttons are.
    regions = gba_input.TOUCH_REGIONS
    ui_regions = gba_ui.button_regions()
    check(len(regions) == 6, "six touch regions defined")
    check([(n, x, y, w, h) for (n, x, y, w, h, _b) in regions] == ui_regions,
          "gba_input's touch table equals the shared gba_ui table")
    check(set(name for name, *_rest in regions) ==
          {"A", "B", "L", "R", "Start", "Select"},
          "touchscreen exposes exactly six action/menu buttons, no D-pad")
    check(all(not (bit & 0x0F0) for *_region, bit in regions),
          "touchscreen never generates physical joystick direction bits")

    # Press the centre of each region and expect its bit.
    for name, rx, ry, rw, rh, bit in regions:
        cx, cy = rx + rw // 2, ry + rh // 2
        install_stubs(touch_points=[FakeTouchPoint(cx, cy, gba_input.EVENT_DOWN)])
        sys.modules.pop("gba_input", None)
        import gba_input as gi2
        got = gi2.GBAInput().read_touch()
        check(got == bit, "touch centre of %-6s => 0x%03X" % (name, bit))

    # A release event must NOT register as a press.
    name, rx, ry, rw, rh, bit = regions[0]
    install_stubs(touch_points=[FakeTouchPoint(rx + 2, ry + 2, gba_input.EVENT_UP)])
    sys.modules.pop("gba_input", None)
    import gba_input as gi3
    got = gi3.GBAInput().read_touch()
    check(got == 0, "EVENT_UP at %s centre => no press" % name)

    # A point outside every region registers nothing.
    install_stubs(touch_points=[FakeTouchPoint(400, 240, gba_input.EVENT_DOWN)])
    sys.modules.pop("gba_input", None)
    import gba_input as gi4
    got = gi4.GBAInput().read_touch()
    check(got == 0, "touch in the middle of the screen => no button")

    # An out-of-range coordinate is discarded.
    install_stubs(touch_points=[FakeTouchPoint(9999, 9999, gba_input.EVENT_DOWN)])
    sys.modules.pop("gba_input", None)
    import gba_input as gi5
    got = gi5.GBAInput().read_touch()
    check(got == 0, "out-of-range touch coordinate is discarded")

    # The screen must support the common shoulder/action combinations. A
    # single-button centre test cannot catch implementations returning only
    # the first point (or accidentally replacing an earlier mask).
    for first in range(len(regions)):
        for second in range(first + 1, len(regions)):
            pair = (regions[first], regions[second])
            points = [FakeTouchPoint(x + w // 2, y + h // 2,
                                    gba_input.EVENT_DOWN)
                      for _name, x, y, w, h, _bit in pair]
            install_stubs(touch_points=points)
            sys.modules.pop("gba_input", None)
            import gba_input as gi_pair
            got = gi_pair.GBAInput().read_touch()
            check(got == pair[0][-1] | pair[1][-1],
                  "simultaneous touch %s + %s preserves both keys"
                  % (pair[0][0], pair[1][0]))

    action = next(region for region in regions if region[0] == "A")
    _name, x, y, w, h, bit = action
    install_stubs(adc_values={0: 4000, 1: 100},
                  touch_points=[FakeTouchPoint(x + w // 2, y + h // 2,
                                              gba_input.EVENT_DOWN)])
    sys.modules.pop("gba_input", None)
    import gba_input as gi_combined
    check(gi_combined.GBAInput().read_keys() == bit | (1 << 4) | (1 << 7),
          "physical joystick diagonal and virtual A combine without conflict")


def test_layout_consistency():
    section("2b. gba_ui: every layout is self-consistent and in bounds")
    import importlib
    import gba_ui
    importlib.reload(gba_ui)

    for layout in ("full", "split"):
        scale, off_x, off_y, disp_x, disp_y = gba_ui.layout_params(layout)
        # The scaler only supports 1/2/3x and must stay inside the 720x480 buffer.
        out_w, out_h = 240 * scale, 160 * scale
        check(1 <= scale <= 3, "%s: scale %d is valid" % (layout, scale))
        check(off_x >= 0 and off_y >= 0 and
              off_x + out_w <= gba_ui.DISP_W and off_y + out_h <= gba_ui.DISP_H,
              "%s: scaled game area (%dx%d at %d,%d) fits the buffer"
              % (layout, out_w, out_h, off_x, off_y))
        # The whole buffer must fit on the panel.
        check(disp_x >= 0 and disp_x + gba_ui.DISP_W <= gba_ui.LCD_W,
              "%s: 720-wide buffer fits on the 800-wide panel at x=%d"
              % (layout, disp_x))

        for name, x, y, w, h in gba_ui.button_regions(layout):
            check(0 <= x and x + w <= gba_ui.LCD_W and
                  0 <= y and y + h <= gba_ui.LCD_H,
                  "%s: button %-6s is fully on screen" % (layout, name))

        regions = gba_ui.button_regions(layout)
        check(len(regions) == 6 and
              set(region[0] for region in regions) ==
              {"A", "B", "L", "R", "Start", "Select"},
              "%s: only six requested virtual action/menu buttons exist" % layout)
        collisions = []
        for index, (name, x, y, w, h) in enumerate(regions):
            for other, ox, oy, ow, oh in regions[index + 1:]:
                if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                    collisions.append((name, other))
        check(not collisions,
              "%s: buttons have distinct touch areas without overlap" % layout)

    # In split layout the buttons must NOT overlap the game area (that is the
    # whole point of the layout).
    scale, off_x, off_y, disp_x, _dy = gba_ui.layout_params("split")
    game_right = disp_x + off_x + 240 * scale     # screen-space right edge
    overlaps = []
    for name, x, y, w, h in gba_ui.button_regions("split"):
        if x < game_right:
            overlaps.append(name)
    check(not overlaps,
          "split: no button overlaps the game area (right edge x=%d); offenders=%s"
          % (game_right, overlaps or "none"))


def test_async_touch():
    section("2c. gba_input: asynchronous cache, fallback and cleanup")
    import gba_input

    class AsyncTouch(FakeTouch):
        def __init__(self, fail_start=False):
            super().__init__()
            self.fail_start = fail_start
            self.starts = self.sync_reads = self.cached_reads = self.deinits = 0
            self.fail_cached_read = False

        def start_async(self):
            self.starts += 1
            if self.fail_start:
                raise OSError("touch thread unavailable")

        def read(self, n=1):
            self.sync_reads += 1
            return super().read(n)

        def read_cached(self, n=1):
            self.cached_reads += 1
            if self.fail_cached_read:
                raise OSError("cached snapshot unavailable")
            return tuple(self._points[:n])

        def deinit(self):
            self.deinits += 1

    def create_input(touch):
        with patch.object(gba_input, "TOUCH", lambda index: touch), \
                patch.object(gba_input, "ADC", lambda channel: FakeADC(channel, 2048)):
            return gba_input.GBAInput()

    _name, x, y, width, height, bit = gba_input.TOUCH_REGIONS[0]
    held = FakeTouchPoint(x + width // 2, y + height // 2, gba_input.EVENT_DOWN)
    released = FakeTouchPoint(held.x, held.y, gba_input.EVENT_UP)
    touch = AsyncTouch()
    inp = create_input(touch)
    check(inp.touch_async and touch.starts == 1,
          "available async API starts the worker exactly once")
    touch._points = [held]
    check([inp.read_keys() for _ in range(4)] == [bit] * 4,
          "a held cached point remains pressed across repeated frame reads")
    touch._points = [released]
    check(inp.read_keys() == 0, "a cached release clears the held key")
    touch._points = []
    check(inp.read_keys() == 0, "an empty cached snapshot also clears the key")
    touch.fail_cached_read = True
    check(inp.read_keys() == 0 and touch.sync_reads == 0 and touch.cached_reads == 7,
          "async reads, including cache errors, never call blocking TOUCH.read")
    inp.deinit()
    inp.deinit()
    check(touch.deinits == 1 and inp.tp is None and not inp.touch_async and
          inp.read_touch() == 0,
          "deinit closes the touch worker once and disables stale cached input")

    touch = AsyncTouch()
    inp = create_input(touch)
    _name2, x2, y2, width2, height2, bit2 = gba_input.TOUCH_REGIONS[1]
    touch._points = [released, FakeTouchPoint(x2 + width2 // 2,
                     y2 + height2 // 2, gba_input.EVENT_DOWN)]
    check(inp.read_touch() == bit2,
          "cached multi-touch keeps a held key after another finger releases")
    inp.deinit()

    touch = AsyncTouch(fail_start=True)
    touch._points = [held]
    inp = create_input(touch)
    check(not inp.touch_async and touch.starts == 1 and inp.read_keys() == bit and
          touch.sync_reads == 1 and touch.cached_reads == 0,
          "a failed async start falls back to working synchronous touch input")
    inp.deinit()
    check(touch.deinits == 1, "failed-start touch devices are still closed")

    class PartialAsyncTouch(FakeTouch):
        def __init__(self):
            super().__init__(points=[held])
            self.starts = 0

        def start_async(self):
            self.starts += 1

    touch = PartialAsyncTouch()
    inp = create_input(touch)
    check(not inp.touch_async and touch.starts == 0 and inp.read_keys() == bit,
          "firmware without read_cached keeps the complete synchronous path")
    inp.deinit()
    check(inp.tp is None and inp.read_touch() == 0,
          "legacy touch devices without deinit can still release Python input")


def test_joystick_button():
    section("2d. gba_input: GPIO18 pull-up and debounced press edges")
    import gba_input

    pin_calls, fpioa_calls = [], []

    class TestPin:
        IN = 0
        PULL_UP = 1

        def __init__(self, *args, **kwargs):
            pin_calls.append((args, kwargs))
            self.level = 1

        def value(self):
            return self.level

    class TestFPIOA:
        GPIO0 = 100

        def set_function(self, *args, **kwargs):
            fpioa_calls.append((args, kwargs))

    class CountingADC(FakeADC):
        def __init__(self, channel):
            super().__init__(channel, 4000 if channel == 0 else 2048)
            self.reads = 0

        def read_u16(self):
            self.reads += 1
            return super().read_u16()

    with patch.object(gba_input, "Pin", TestPin), \
            patch.object(gba_input, "FPIOA", TestFPIOA), \
            patch.object(gba_input, "ADC", CountingADC), \
            patch.object(gba_input, "TOUCH", lambda index: FakeTouch()), \
            patch.object(gba_input, "JOY_K_ENABLE", True):
        inp = gba_input.GBAInput()
    check(inp.k_enabled and fpioa_calls == [((18, 118), {"ie": 1, "oe": 0})],
          "K configures GPIO18 as an input through FPIOA")
    args, kwargs = pin_calls[0] if pin_calls else ((), {})
    pull = kwargs.get("pull", args[2] if len(args) > 2 else None)
    check(args[:2] == (18, TestPin.IN) and pull == TestPin.PULL_UP,
          "K opens GPIO18 with an input pull-up")

    levels = [1, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0]
    events = []
    for level in levels:
        inp.k_pin.level = level
        events.append(inp.read_events())
    check([event["k_pressed"] for event in events] ==
          [False, False, False, False, True, False, False, False,
           False, False, True],
          "K rejects one-sample bounce, emits one held edge, then rearms on release")
    check(all(event["mask"] == gba_input.BTN_RIGHT and
              event["x_dir"] == 1 and event["y_dir"] == 0 for event in events),
          "K press edges remain separate from GBA direction masks")
    check(inp.adc_x.reads == len(levels) and inp.adc_y.reads == len(levels),
          "read_events samples each joystick axis exactly once per frame")
    inp.deinit()


def test_control_geometry():
    section("2d. gba_ui: transparent rounded controls and touch silhouettes")
    import gba_ui

    class CaptureCanvas:
        def __init__(self):
            self.calls = []

        def clear(self):
            self.calls.append(("clear", (), {}))

        def draw_circle(self, *args, **kwargs):
            self.calls.append(("circle", args, kwargs))

        def draw_rectangle(self, *args, **kwargs):
            self.calls.append(("rectangle", args, kwargs))

        def draw_string_advanced(self, *args, **kwargs):
            self.calls.append(("text", args, kwargs))

    for layout in ("full", "split"):
        canvas = CaptureCanvas()
        gba_ui.draw_controls(canvas, layout)
        check(canvas.calls[0][0] == "clear" and
              sum(call[0] == "clear" for call in canvas.calls) == 1,
              "%s: controls begin on a cleared transparent background" % layout)
        regions = gba_ui.button_regions(layout)
        labels = [args[-1] for op, args, _kw in canvas.calls if op == "text"]
        check(labels == [name.upper() for name, *_r in regions],
              "%s: each shared action/menu region has exactly one visible label"
              % layout)
        colors = [kw.get("color") for op, _args, kw in canvas.calls
                  if op != "clear"]
        check(all(isinstance(color, tuple) and len(color) == 4 and
                  all(0 <= c <= 255 for c in color) and 0 < color[0] < 255
                  for color in colors),
              "%s: all shape/text colors carry nonopaque ARGB pixel alpha" % layout)
        shape_calls = [call for call in canvas.calls if call[0] in
                       ("circle", "rectangle")]
        check(all(kw.get("fill") is True for _op, _args, kw in shape_calls),
              "%s: controls draw filled silhouettes with visible rims" % layout)

        for name, x, y, w, h in regions:
            contains = lambda px, py: gba_ui.button_contains(name, x, y, w, h, px, py)
            cx, cy = x + (w - 1) // 2, y + (h - 1) // 2
            check(contains(cx, cy) and contains(x, cy) and
                  contains(x + w - 1, cy) and contains(cx, y) and
                  contains(cx, y + h - 1),
                  "%s: %s center and four visible edge midpoints are touchable"
                  % (layout, name))
            check(not any(contains(px, py) for px, py in
                          ((x, y), (x + w - 1, y), (x, y + h - 1),
                           (x + w - 1, y + h - 1), (x - 1, cy),
                           (x + w, cy), (cx, y - 1), (cx, y + h))),
                  "%s: %s transparent corners/outside points never activate it"
                  % (layout, name))
            label = next(args for op, args, _kw in canvas.calls
                         if op == "text" and args[-1] == name.upper())
            tx, ty, size, text = label
            check(x <= tx and tx + len(text) * size * 3 // 5 <= x + w and
                  y <= ty and ty + size <= y + h,
                  "%s: %s label fits its shared button region" % (layout, name))

        circles = [args for op, args, _kw in shape_calls if op == "circle"]
        check(all((x + (w - 1) // 2, y + (h - 1) // 2, (h - 1) // 2)
                  in circles for name, x, y, w, h in regions if name in ("A", "B")),
              "%s: A/B circle centers and radii match touch screen coordinates"
              % layout)


# --------------------------------------------------------------------------
# 3. gba_audio: whole-block accumulation (the underrun fix)
# --------------------------------------------------------------------------
def test_audio():
    section("3. gba_audio: whole blocks, startup lead and continuous playout")

    class FakeClock:
        def __init__(self):
            self.us = 0

        def ticks_us(self):
            return self.us

        def ticks_diff(self, newer, older):
            return newer - older

        def advance_to(self, us):
            assert us >= self.us, "the audio test clock must be monotone"
            self.us = us

    clock = FakeClock()

    # Fake exactly what pyaudio.Write_stream.write() does: it plays
    # frame.len bytes regardless of how many were supplied (short data is
    # zero-padded by the by-ref bytearray slice assignment).
    class FakeStream:
        def __init__(self, block_bytes, rate, start):
            self.block_bytes = block_bytes
            self.rate = rate
            self.writes = []
            self.payloads = []
            self.return_code = 0
            self.raise_on_write = False
            self.started = False
            self.start_calls = 0
            self.stop_calls = 0
            self.volume_calls = []
            self.volume_value = None
            self.volume_return_code = 0
            self.raise_on_volume_set = False
            self.raise_on_volume_read = False
            self.open_start = start
            self.queue_samples = 0.0
            self.last_us = clock.us
            self.first_send = False
            self.underruns = 0
            self.missing_samples = 0.0
            if start:
                self.start_stream()

        def consume(self):
            # AO keeps consuming sound between pump calls. Counting begins
            # after the first successful send; initial hardware startup silence
            # is distinct from an interruption in an established PCM stream.
            if self.started and self.first_send:
                samples = (clock.us - self.last_us) * self.rate / 1000000.0
                missing = max(0.0, samples - self.queue_samples)
                if missing > 0.000001:
                    self.underruns += 1
                    self.missing_samples += missing
                self.queue_samples = max(0.0, self.queue_samples - samples)
            self.last_us = clock.us

        def start_stream(self):
            assert not self.started, "AO must start exactly once"
            self.start_calls += 1
            self.started = True
            self.last_us = clock.us

        def write(self, data):
            assert self.started, "PCM must not be sent before AO starts"
            self.consume()
            clock.us += 100
            self.consume()
            if self.raise_on_write:
                raise OSError("AO unavailable")
            # Record what the BOARD would actually play: always block_bytes.
            played = len(data)
            if played < self.block_bytes:
                # short write => the rest is silence, but the full block plays
                played = self.block_bytes
            self.writes.append((len(data), played))
            if self.return_code is None or self.return_code == 0:
                self.payloads.append(bytes(data))
                self.queue_samples += played / 4.0
                self.first_send = True
            return self.return_code

        def volume(self, vol=None):
            assert self.started, "codec volume is available only after AO starts"
            self.volume_calls.append(vol)
            if vol is not None:
                if self.raise_on_volume_set:
                    raise OSError("codec volume setter unavailable")
                if self.volume_return_code is False:
                    return False
                self.volume_value = vol
                return self.volume_return_code
            if self.raise_on_volume_read:
                raise OSError("codec volume readback unavailable")
            return self.volume_value

        def stop_stream(self):
            self.consume()
            self.stop_calls += 1
            self.started = False

        def close(self):
            pass

    class FakePyAudio:
        def __init__(self):
            self.stream = None

        def initialize(self, chunk):
            pass

        def open(self, **kw):
            self.stream = FakeStream(kw["frames_per_buffer"] * 2 * 2,
                                     kw["rate"], kw.get("start", True))
            return self.stream

        def terminate(self):
            pass

    # Install a fake media.pyaudio package.
    media = types.ModuleType("media")
    pyaudio_mod = types.ModuleType("media.pyaudio")
    pyaudio_mod.PyAudio = FakePyAudio
    pyaudio_mod.paInt16 = 0
    media.pyaudio = pyaudio_mod
    sys.modules["media"] = media
    sys.modules["media.pyaudio"] = pyaudio_mod

    sys.modules.pop("gba_audio", None)
    import gba_audio
    import importlib
    importlib.reload(gba_audio)
    gba_audio.time = clock

    class FakeGBA:
        """Produces ~738 samples per frame, like the real core at 44100 Hz."""

        def __init__(self, per_frame_samples, sample=0):
            self.per_frame = per_frame_samples
            self.residual = 0
            self.sample = sample

        def audio_avail(self):
            # emulate the core: data becomes available each frame
            self.residual += self.per_frame
            return self.residual

        def audio_frame(self):
            n = self.residual
            self.residual = 0
            return bytes([self.sample]) * (n * 2 * 2)  # interleaved stereo int16

    g = gba_audio.GBAudio(rate=44100, frames_per_buffer=1024)
    ok = g.init()
    check(ok, "GBAudio.init() succeeds against the fake PyAudio")
    stream = g.p.stream
    block = 1024 * 2 * 2
    check(stream.open_start is False and stream.start_calls == 0 and
          not stream.started and not g.stats()["started"],
          "init opens AO stopped and does not enable playback without PCM")
    check(not stream.volume_calls,
          "init waits for AO startup before setting or reading codec volume")
    check(g.stats()["prebuffer_blocks"] == 3,
          "the startup target is three complete PCM blocks")

    # Volume API accepts arbitrary input but always stores a clamped request;
    # before AO starts it must not touch the hardware stream.
    check(g.set_volume(-25) and g.stats()["volume_level"] == 0,
          "pre-start volume request clamps negative values to zero")
    check(g.set_volume(140) and g.stats()["volume_level"] == 100,
          "pre-start volume request clamps values above one hundred")
    check(not stream.volume_calls,
          "pre-start volume requests do not call the stopped AO stream")
    check(g.set_volume(80) and g.stats()["volume_level"] == 80,
          "volume request can be restored before the first AO start")

    # Feed 100 simulated frames of 738 samples each.
    fake = FakeGBA(738)
    total_written = 0
    for frame in range(1, 101):
        sent = g.pump(fake)
        total_written += sent
        if frame == 4:
            check(sent == 0 and not stream.started and not stream.writes and
                  g.stats()["pending_bytes"] == 4 * 738 * 4,
                  "four GBA frames stay buffered below the three-block threshold")
        if frame == 5:
            check(sent == 3 * block and stream.start_calls == 1 and
                  len(stream.writes) == 3 and g.stats()["started"],
                  "frame five starts AO once and immediately submits all three blocks")
            check(stream.volume_calls == [80, None] and
                  g.stats()["volume_read"] == 80,
                  "volume 80 is applied and read back after AO startup")

    check(len(stream.writes) > 0, "audio blocks were written")
    bad = [w for w in stream.writes if w[0] != block]
    check(not bad,
          "every write was exactly one block (%d writes, %d bytes each)"
          % (len(stream.writes), block))
    if bad:
        print("      offending writes (sent, played): %s" % bad[:5])

    produced = 738 * 100 * 2 * 2
    check(total_written <= produced,
          "never wrote more audio than was produced (%d <= %d)"
          % (total_written, produced))
    # With 73800 samples produced and 1024-sample blocks, we expect 72 blocks.
    expect_blocks = (738 * 100) // 1024
    check(len(stream.writes) == expect_blocks,
          "wrote floor(produced/block) = %d blocks (got %d)"
          % (expect_blocks, len(stream.writes)))

    st = g.stats()
    check(st["pending_bytes"] < block,
          "residual stays below one block (%d bytes)" % st["pending_bytes"])
    check(st["pcm_zero_chunks"] == 100 and st["pcm_nonzero_chunks"] == 0,
          "silent source chunks are identified instead of mistaken for sound")
    check(st["max_write_us"] == 100 and st["max_send_gap_us"] == 100,
          "send diagnostics measure successful calls using the supplied clock")

    before_volume_calls = list(stream.volume_calls)
    check(g.set_volume(80) is True and stream.volume_calls == before_volume_calls,
          "setting the current volume succeeds without calling the codec")
    stream.volume_return_code = False
    check(g.set_volume(65) is False and
          (g.stats()["volume_level"], g.stats()["volume_read"], stream.volume_value) ==
          (80, 80, 80) and stream.volume_calls == before_volume_calls + [65],
          "a false codec setter preserves requested volume and readback")
    stream.volume_return_code = 0
    stream.raise_on_volume_set = True
    before_volume_calls = list(stream.volume_calls)
    check(g.set_volume(65) is False and
          (g.stats()["volume_level"], g.stats()["volume_read"], stream.volume_value) ==
          (80, 80, 80) and stream.volume_calls == before_volume_calls + [65],
          "a codec setter exception preserves requested volume and readback")
    stream.raise_on_volume_set = False
    stream.raise_on_volume_read = True
    before_volume_calls = list(stream.volume_calls)
    check(g.set_volume(65) is True and g.stats()["volume_level"] == 65 and
          stream.volume_value == 65 and stream.volume_calls == before_volume_calls + [65, None],
          "successful codec set remains successful when readback raises")
    before_volume_calls = list(stream.volume_calls)
    check(g.set_volume(65) is True and stream.volume_calls == before_volume_calls,
          "repeating the successful target skips setter and failing getter")
    stream.raise_on_volume_read = False
    check(g.set_volume(80) is True and g.stats()["volume_level"] == 80 and
          g.stats()["volume_read"] == 80 and stream.start_calls == 1 and
          stream.stop_calls == 0,
          "volume changes restore normal readback without restarting the audio stream")

    # AO returns its send status; a completed Python call need not mean
    # the hardware accepted the audio frame.
    g._accum = bytearray()
    before = g.stats()["blocks_written"]
    stream.return_code = -1
    check(g.pump(FakeGBA(1024, sample=1)) == 0,
          "an AO error return reports zero successfully written bytes")
    st = g.stats()
    check(st["blocks_written"] == before and st["blocks_failed"] == 1,
          "AO errors increment failed blocks, never successful blocks")
    check(st["last_write_rc"] == -1 and st["pcm_nonzero_chunks"] == 1,
          "nonzero PCM and the failing AO status remain visible in diagnostics")

    stream.return_code = None
    check(g.pump(FakeGBA(1024)) == block,
          "compatible streams returning None still count as successful sends")
    stream.raise_on_write = True
    check(g.pump(FakeGBA(1024)) == 0 and g.stats()["blocks_failed"] == 2,
          "write exceptions are counted as failed sends")
    stream.raise_on_write = False
    stream.return_code = 0

    # Backlog protection: a huge surplus must be dropped, not accumulated.
    huge = FakeGBA(200000)
    g.pump(huge)
    check(len(g._accum) <= gba_audio.MAX_ACCUM_BYTES,
          "backlog is capped at MAX_ACCUM_BYTES (%d)" % gba_audio.MAX_ACCUM_BYTES)
    check(g.stats()["blocks_dropped"] > 0, "over-production is counted as dropped")

    g.deinit()
    check(not g.stats()["started"] and not stream.started and
          stream.stop_calls == 1 and g.stats()["pending_bytes"] == 0,
          "deinit stops playback once and resets startup and accumulation state")

    def run_cadence(prebuffer_blocks):
        nonlocal clock
        clock = FakeClock()
        gba_audio.time = clock
        with patch.object(gba_audio, "PREBUFFER_BLOCKS", prebuffer_blocks):
            sound = gba_audio.GBAudio(rate=44100, frames_per_buffer=1024)
            assert sound.init()
            sink = sound.p.stream
            produced_chunks = []
            send_frames = []
            started_frame = None
            for frame in range(1, 601):
                # A monotone frame timeline with 0..10 ms of bounded arrival
                # jitter, rather than an unrealistically instantaneous sink.
                jitter_us = ((frame * 37) % 101) * 100
                clock.advance_to(round(frame * 1000000.0 / 59.7275) + jitter_us)
                pcm = bytes([1 + frame % 251]) * (738 * 4)
                produced_chunks.append(pcm)
                if sound.pump(FakeGBA(738, sample=1 + frame % 251)):
                    send_frames.append(frame)
                    if started_frame is None:
                        started_frame = frame
                sink.consume()
            produced = b"".join(produced_chunks)
            delivered = b"".join(sink.payloads)
            remaining = bytes(sound._accum)
            snapshot = sound.stats()
            sound.deinit()
            return {"stream": sink, "stats": snapshot,
                    "produced": produced, "delivered": delivered,
                    "remaining": remaining, "send_frames": send_frames,
                    "started_frame": started_frame}

    buffered = run_cadence(3)
    unbuffered = run_cadence(1)
    sink = buffered["stream"]
    check(buffered["started_frame"] == 5 and sink.start_calls == 1,
          "clocked playout begins on the fifth 738-sample frame with three blocks")
    check(all(sent == block and played == block for sent, played in sink.writes),
          "the clocked sink always receives complete 1024-sample stereo blocks")
    check(buffered["delivered"] + buffered["remaining"] == buffered["produced"],
          "startup burst and later sends preserve every PCM byte in source order")
    frame_gaps = {newer - older for older, newer in
                  zip(buffered["send_frames"], buffered["send_frames"][1:])}
    check(frame_gaps == {1, 2},
          "738 samples per GBA frame exercise both one- and two-frame send intervals")
    check(sink.underruns == 0 and sink.missing_samples == 0 and
          buffered["stats"]["blocks_failed"] == 0 and
          buffered["stats"]["blocks_dropped"] == 0,
          "three-block lead prevents underruns throughout 600 frames with <=10ms jitter")
    check(unbuffered["stream"].underruns > 0 and
          unbuffered["stream"].missing_samples > 0,
          "the same consuming sink detects real gaps without startup lead")
    check(buffered["stats"]["max_send_gap_us"] > 23000 and
          buffered["stats"]["max_write_us"] == 100,
          "diagnostics expose long send gaps even when queued sound keeps playing")


# --------------------------------------------------------------------------
# 4. save_if_idle: must skip while the emulated flash is busy
# --------------------------------------------------------------------------
def test_save_guard():
    section("4. save_if_idle: skips while the emulated flash is busy")

    # Import main.py with heavy stubs so we can call save_if_idle directly.
    image = types.ModuleType("image")
    image.Image = object
    image.RGB565 = 1
    image.ARGB8888 = 2
    sys.modules["image"] = image

    media_display = types.ModuleType("media.display")
    media_display.Display = types.SimpleNamespace()
    media_media = types.ModuleType("media.media")
    media_media.MediaManager = types.SimpleNamespace()
    sys.modules["media.display"] = media_display
    sys.modules["media.media"] = media_media
    sys.modules.setdefault("media", types.ModuleType("media"))

    sys.modules.pop("main", None)
    import importlib.util
    spec = importlib.util.spec_from_file_location("board_main", SRC / "main.py")
    mod = importlib.util.module_from_spec(spec)
    # main.py calls main() at import; strip the trailing call by executing the
    # source without the final "main()" line.
    src = (SRC / "main.py").read_text(encoding="utf-8")
    src = src.rstrip()
    assert src.endswith("main()"), "expected main.py to end with a main() call"
    src = src[: -len("main()")]
    exec(compile(src, "board_main.py", "exec"), mod.__dict__)

    class FakeGBA:
        def __init__(self, busy):
            self.busy = busy
            self.writes = 0

        def save_busy(self):
            return self.busy

        def save_write(self, path):
            self.writes += 1
            return 131072

    busy = FakeGBA(True)
    mod.save_if_idle(busy, "test")
    check(busy.writes == 0, "busy flash => save_write is NOT called")

    idle = FakeGBA(False)
    mod.save_if_idle(idle, "test")
    check(idle.writes == 1, "idle flash => save_write IS called once")

    # A firmware without save_busy() must still save (backwards compatible).
    class LegacyGBA:
        def __init__(self):
            self.writes = 0

        def save_write(self, path):
            self.writes += 1
            return 131072

    legacy = LegacyGBA()
    mod.save_if_idle(legacy, "test")
    check(legacy.writes == 1, "no save_busy() => save still happens")


def test_frame_loop_timing():
    section("5. run_emulator: fractional pacing, timing and input cleanup")

    def run_loop(profile, frames=120, run_us=2000, start_us=0,
                 tick_period=None, startup_failure=None, controls=False,
                 real_display=False, input_events=None, audio_enabled=True):
        # Execute the production function, suppressing the boot entry and
        # redirecting even its top-level log truncation to memory. No ROM,
        # save, log, or media device is opened by this harness.
        mod = types.ModuleType("board_main_loop_test")
        mod.open = lambda *args, **kwargs: io.StringIO()
        src = (SRC / "main.py").read_text(encoding="utf-8").rstrip()
        assert src.endswith("main()"), "expected the board boot entry"
        exec(compile(src[:-len("main()")], "board_main.py", "exec"),
             mod.__dict__)

        class FakeClock:
            def __init__(self):
                self.us = start_us
                self.us_reads = 0
                self.sleeps = []

            def ticks_us(self):
                self.us_reads += 1
                if not profile:
                    raise AssertionError("profiling off must not read ticks_us")
                return self.us if tick_period is None else self.us % tick_period

            def ticks_ms(self):
                value = self.us // 1000
                return value if tick_period is None else value % tick_period

            @staticmethod
            def ticks_diff(a, b):
                if tick_period is None:
                    return a - b
                half = tick_period // 2
                return (a - b + half) % tick_period - half

            @staticmethod
            def ticks_add(a, b):
                return a + b if tick_period is None else (a + b) % tick_period

            def sleep_ms(self, ms):
                self.sleeps.append(ms)
                self.us += ms * 1000

        clock = FakeClock()
        events, messages, errors = [], [], []
        input_snapshots, volume_overlays = [], []

        class FakeGBA:
            def __init__(self):
                self.frames = 0
                self.key_masks = []
                self.frame_masks = []
                self.current_mask = 0
                self.saved_paths = []
                self.loaded_paths = []
                self.unloads = 0
                self.scale_args = []

            def load(self, path):
                self.loaded_paths.append(path)
                if startup_failure == "load exception":
                    raise OSError("ROM open failed")
                return -1 if startup_failure == "load status" else 0

            def save_type(self):
                return "FLASH1M"

            def save_size(self):
                return 131072

            def save_load(self, path):
                self.loaded_paths.append(path)
                return 131072

            def save_busy(self):
                return 0

            def save_write(self, path):
                self.saved_paths.append(path)
                return 131072

            def set_audio_rate(self, rate):
                return rate

            def display_alloc(self):
                return 0

            def disp_bytes(self):
                return bytearray(mod.DISP_W * mod.DISP_H * 2)

            def set_keys(self, mask):
                self.key_masks.append(mask)
                self.current_mask = mask
                clock.us += 500

            def run_frame(self):
                events.append("run")
                self.frame_masks.append(self.current_mask)
                clock.us += run_us
                self.frames += 1
                return self.frames

            def scale_into(self, *args):
                events.append("scale")
                self.scale_args.append(args)
                clock.us += 3000
                return 0

            def unload(self):
                self.unloads += 1

        gba = FakeGBA()

        class FakeInput:
            def __init__(self):
                self.deinits = 0
                self.event_index = 0
                input_instances.append(self)

            def read_events(self):
                events.append("input")
                clock.us += 400
                if callable(input_events):
                    value = dict(input_events(gba.frames, (clock.us - start_us) / 1000.0))
                    input_snapshots.append((gba.frames, clock.us / 1000.0, value))
                    return value
                if input_events is not None and self.event_index < len(input_events):
                    value = dict(input_events[self.event_index])
                    self.event_index += 1
                    input_snapshots.append((gba.frames, clock.us / 1000.0, value))
                    return value
                if input_events is not None:
                    return {"mask": 0, "x_dir": 0, "y_dir": 0, "k_pressed": False}
                return {"mask": 1 + gba.frames % 2, "x_dir": 0,
                        "y_dir": 0, "k_pressed": False}

            def read_keys(self):
                events.append("input")
                clock.us += 400
                return 1 + gba.frames % 2

            def deinit(self):
                self.deinits += 1

        input_instances = []

        class FakeAudio:
            def __init__(self, rate):
                self.pumps = 0
                self.deinits = 0
                self.volume_level = 80
                self.volume_calls = []
                self.set_volume_calls = []
                self.volume_call_times = []

            def init(self):
                return True

            def set_volume(self, value):
                self.volume_call_times.append(clock.us / 1000.0)
                self.set_volume_calls.append(int(value))
                value = max(0, min(100, int(value)))
                if value == self.volume_level:
                    return False
                self.volume_level = value
                self.volume_calls.append(value)
                return True

            def pump(self, core):
                assert core is gba
                events.append("audio")
                self.pumps += 1
                clock.us += 5000

            def stats(self):
                return {"blocks_written": self.pumps,
                        "blocks_dropped": 0, "pending_bytes": 0}

            def deinit(self):
                self.deinits += 1

        audio_instances = []

        def make_audio(rate):
            audio = FakeAudio(rate)
            audio_instances.append(audio)
            return audio

        def exitpoint(*args):
            # Stop only at the next frame's exitpoint: every frame must
            # complete audio, pacing, and interval logging before cleanup.
            if args:
                return
            if gba.frames == frames:
                raise KeyboardInterrupt
            clock.us += 100

        display_calls = []
        submissions = []
        release_snapshots = []
        drawing_calls = []
        overlay_liveness_during_game = []
        lifecycle = []
        init_calls = []
        native_buffer_bytes = 0

        def show_image(img, **kwargs):
            nonlocal native_buffer_bytes
            image_bytes = img.width * img.height * (2 if img.pixel_format == 1 else 4)
            if native_buffer_bytes == 0:
                native_buffer_bytes = image_bytes
            if real_display and image_bytes > native_buffer_bytes:
                raise AssertionError("vendor rotated buffer would be too small")
            if img.pixel_format == 1:
                events.append("show")
                display_calls.append(kwargs)
                overlay_liveness_during_game.append([ref() is not None
                    for args, _kwargs, ref in image_calls if args[2] == 2])
            elif startup_failure == "overlay show":
                raise OSError("OSD overlay submission failed")
            submissions.append((img.ref, kwargs.copy(), gba.frames))
            clock.us += 4000

        image_calls = []

        class FakeImage:
            def __init__(self, width, height, pixel_format):
                self.width = width
                self.height = height
                self.pixel_format = pixel_format
                self.ref = weakref.ref(self)

            def _draw(self, operation, args, kwargs):
                if startup_failure == "overlay draw" and self.pixel_format == 2:
                    raise RuntimeError("OSD overlay drawing failed")
                drawing_calls.append((self.ref, operation, args, kwargs.copy(),
                                      gba.frames))
                return self

            def clear(self, *args, **kwargs):
                return self._draw("clear", args, kwargs)

            def draw_rectangle(self, *args, **kwargs):
                return self._draw("rectangle", args, kwargs)

            def draw_circle(self, *args, **kwargs):
                return self._draw("circle", args, kwargs)

            def draw_string_advanced(self, *args, **kwargs):
                return self._draw("text", args, kwargs)

        def make_image(*args, **kwargs):
            if (startup_failure == "image" and args[2] == 1) or (
                    startup_failure == "overlay allocation" and args[2] == 2):
                raise MemoryError("image allocation failed")
            img = FakeImage(*args[:3])
            image_calls.append((args, kwargs, img.ref))
            return img

        def release_display():
            release_snapshots.append([ref() is not None
                                      for _args, _kwargs, ref in image_calls])

        def display_init(*args, **kwargs):
            lifecycle.append("display init")
            init_calls.append((args, kwargs.copy()))
            if startup_failure == "display":
                raise OSError("display initialization failed")

        def display_deinit():
            lifecycle.append("display deinit")
            release_display()

        mod.time = clock
        mod.os = types.SimpleNamespace(exitpoint=exitpoint,
                                       EXITPOINT_ENABLE_SLEEP=2)
        mod._image = types.SimpleNamespace(Image=make_image, RGB565=1, ARGB8888=2)
        mod.Display = types.SimpleNamespace(show_image=show_image,
                                            init=display_init,
                                            deinit=display_deinit, ST7701=1,
                                            LAYER_OSD0=3, LAYER_OSD1=4,
                                            LAYER_OSD2=5, LAYER_OSD3=6)
        mod.MediaManager = types.SimpleNamespace(
            init=lambda: lifecycle.append("media init"),
            deinit=lambda: lifecycle.append("media deinit"))
        real_update_volume = mod.update_volume_overlay

        def update_volume(volume, visible):
            volume_overlays.append((volume, visible, gba.frames, clock.us / 1000.0))
            if real_display:
                real_update_volume(volume, visible)

        mod.update_volume_overlay = update_volume
        if not real_display:
            mod.init_display = lambda tag, controls=False: startup_failure != "display"
            mod.release_display = release_display
        mod.show_message = lambda *args: None
        mod.log = messages.append
        # Retaining exception objects would retain their traceback frames and
        # image buffers, masking the production lifetime/cleanup behavior.
        mod.log_exception = lambda tag, exc: errors.append((tag, repr(exc)))
        mod.PROFILE_TIMING = profile
        mod.FORCE_KEYS = None
        mod.AUDIO_ENABLE = audio_enabled
        mod.SHOW_CONTROLS = controls
        mod.FPS_LOG_EVERY = 120
        mod.SAVE_EVERY_FRAMES = 0
        mod.ROM_PATH = "/test/in-memory/pokemon_emerald.gba"
        mod.SAVE_PATH = "/test/in-memory/pokemon_emerald.sav"
        fake_input = types.ModuleType("gba_input")
        fake_input.GBAInput = FakeInput
        fake_audio = types.ModuleType("gba_audio")
        fake_audio.GBAudio = make_audio
        with patch.dict(sys.modules, {"gba_input": fake_input,
                                      "gba_audio": fake_audio}):
            mod.run_emulator(gba)
        gc.collect()
        return {"mod": mod, "gba": gba, "clock": clock,
                "events": events, "messages": messages, "errors": errors,
                "audio": audio_instances, "display": display_calls,
                "images": image_calls, "inputs": input_instances,
                "drawing": drawing_calls, "submissions": submissions,
                "release_snapshots": release_snapshots,
                "overlay_liveness": overlay_liveness_during_game,
                "lifecycle": lifecycle, "init_calls": init_calls,
                "native_buffer_bytes": native_buffer_bytes,
                "input_snapshots": input_snapshots,
                "volume_overlays": volume_overlays,
                "elapsed_us": clock.us - start_us}

    runs = {}
    for profile in (True, False):
        result = run_loop(profile)
        runs[profile] = result
        mode = "profile on" if profile else "profile off"
        gba, clock = result["gba"], result["clock"]
        check(not result["errors"], "%s: no swallowed loop exception" % mode)
        check(result["events"] == ["input", "run", "scale", "show", "audio"] * 120,
              "%s: all five operations run in order exactly 120 times" % mode)
        check(gba.frames == 120 and gba.key_masks == [1, 2] * 60,
              "%s: 120 frames retain the changed-key updates" % mode)
        params = result["mod"].layout_params()
        check(gba.scale_args == [params[:3]] * 120 and
              result["display"] == [{"x": params[3], "y": params[4],
                                      "layer": result["mod"].Display.LAYER_OSD0}] * 120,
              "%s: shared layout reaches the scaler and display unchanged" % mode)
        image_args, image_kw, _image_ref = result["images"][0]
        check(image_args[:2] == (720, 480) and image_kw["alloc"] == 4,
              "%s: the fixed 720-wide reference image is retained" % mode)
        check(clock.us == 2009000 and set(clock.sleeps) == {1, 2} and
              sum(clock.sleeps) == 209,
              "%s: fractional pacing totals 2009 ms for 120 frames" % mode)
        check(len(result["audio"]) == 1 and result["audio"][0].pumps == 120 and
              result["audio"][0].deinits == 1 and gba.unloads == 1,
              "%s: audio and core finish with their normal cleanup" % mode)
        check(len(result["inputs"]) == 1 and result["inputs"][0].deinits == 1,
              "%s: exiting the loop closes the input worker exactly once" % mode)
        check(gba.saved_paths == ["/test/in-memory/pokemon_emerald.sav"] and
              gba.loaded_paths == ["/test/in-memory/pokemon_emerald.gba",
                                   "/test/in-memory/pokemon_emerald.sav"],
              "%s: only fake ROM/save operations touch isolated paths" % mode)
        fps_lines = [msg for msg in result["messages"] if msg.startswith("frame ")]
        check(len(fps_lines) == 1 and fps_lines[0].startswith("frame 120, 59.7 FPS"),
              "%s: the 120-frame FPS report uses elapsed paced time" % mode)

    timing = [msg for msg in runs[True]["messages"] if msg.startswith("timing ")]
    check(len(timing) == 1 and timing[0].startswith("timing frame 120:"),
          "profile on: exactly one timing report after 120 completed frames")
    measured = dict((name, float(value))
                    for name, value in re.findall(r"(\w+)=(\d+\.\d+)ms",
                                                   timing[0] if timing else ""))
    expected = {"input": 1.0, "run": 2.0, "scale": 3.0, "show": 4.0,
                "audio": 5.0, "work": 15.0, "total": 16.74}
    check(measured == expected,
          "profile on: all five segment averages plus work/total are exact")
    check(not any(msg.startswith("timing ") for msg in runs[False]["messages"]) and
          runs[False]["clock"].us_reads == 0,
          "profile off: no timing report or ticks_us dependency")
    check(runs[True]["events"] == runs[False]["events"] and
          runs[True]["clock"].sleeps == runs[False]["clock"].sleeps,
          "profiling leaves operation order, counts, and pacing unchanged")

    long_run = run_loop(False, frames=36000)
    ideal_us = 36000 * 1000000 / long_run["mod"].TARGET_FPS
    check(0 <= ideal_us - long_run["elapsed_us"] < 1000 and
          not long_run["errors"],
          "36000 paced frames stay within 1 ms of the target duration")
    reports = [line for line in long_run["messages"] if line.startswith("frame ")]
    check(len(reports) == 300 and all("59.7 FPS" in line for line in reports),
          "every long-run interval reports 59.7 FPS without accumulated drift")

    slow_run = run_loop(True, frames=600, run_us=67000)
    check(slow_run["elapsed_us"] == 600 * 80000 and
          slow_run["clock"].sleeps == [] and not slow_run["errors"],
          "an 80 ms frame never adds sleep, even after catchup deadlines reset")

    wrapped = run_loop(True, start_us=((1 << 30) - 1000) * 1000,
                       tick_period=1 << 30)
    check(wrapped["elapsed_us"] == runs[True]["elapsed_us"] and
          wrapped["clock"].sleeps == runs[True]["clock"].sleeps and
          not wrapped["errors"],
          "ticks_ms wrap preserves frame deadlines and pacing exactly")
    wrapped_reports = [line for line in wrapped["messages"]
                       if line.startswith(("frame ", "timing "))]
    plain_reports = [line for line in runs[True]["messages"]
                     if line.startswith(("frame ", "timing "))]
    check(wrapped_reports == plain_reports,
          "FPS and segment timing reports remain correct across ticks wrap")

    for failure in ("load exception", "load status", "display", "image"):
        result = run_loop(False, startup_failure=failure)
        check(result["gba"].frames == 0 and len(result["inputs"]) == 1 and
              result["inputs"][0].deinits == 1,
              "startup failure (%s) still closes the input worker" % failure)
        if failure == "image":
            check(len(result["audio"]) == 1 and
                  result["audio"][0].deinits == 1 and
                  result["gba"].unloads == 1 and
                  result["gba"].saved_paths == [],
                  "image packaging failure releases audio/core without rewriting saves")

    # Volume-control contract: K toggles on its press edge, X adjusts only
    # while the window is open, and direction bits pass through after close.
    volume_events = [
        {"mask": 0, "x_dir": 0, "y_dir": 0, "k_pressed": True},
        {"mask": 0, "x_dir": 1, "y_dir": 0, "k_pressed": False},
        {"mask": 0, "x_dir": -1, "y_dir": 0, "k_pressed": False},
        {"mask": 0, "x_dir": 1, "y_dir": 0, "k_pressed": False},
        {"mask": 0, "x_dir": 0, "y_dir": 0, "k_pressed": True},
        {"mask": 1 << 4, "x_dir": 1, "y_dir": 0, "k_pressed": False},
    ]
    volume_run = run_loop(False, frames=len(volume_events),
                          input_events=volume_events)
    check(not volume_run["errors"],
          "volume event sequence completes without loop exceptions")
    volume_audio = volume_run["audio"][0] if volume_run["audio"] else None
    check(volume_audio is not None and volume_audio.set_volume_calls == [85, 80, 85],
          "K edge toggles window and X right/left changes by five")
    check(volume_audio is not None and volume_audio.volume_calls == [85, 80, 85],
          "set_volume is called only when the target volume changes")
    check(volume_audio is not None and all(0 <= value <= 100
          for value in volume_audio.set_volume_calls),
          "volume values remain within the 0..100 range")
    check(volume_run["gba"].key_masks == [0, 1 << 4],
          "open window consumes joystick X; closed window forwards RIGHT")

    def control_event(x=0, k=False, mask=None):
        if mask is None:
            mask = (1 << 4) if x > 0 else ((1 << 5) if x < 0 else 0)
        return {"mask": mask, "x_dir": x, "y_dir": 0, "k_pressed": k}

    held_right = run_loop(False, frames=100, input_events=lambda frame, ms:
                          control_event(1, k=frame == 0))
    held_audio = held_right["audio"][0]
    check(not held_right["errors"] and held_audio.set_volume_calls == [85, 90, 95, 100],
          "holding X right reaches 100 and never calls set_volume again at the limit")
    times = held_audio.volume_call_times
    check(len(times) == 4 and 400 <= times[1] - times[0] < 418 and
          all(150 <= b - a < 168 for a, b in zip(times[1:], times[2:])),
          "X hold steps immediately, waits 400 ms, then repeats every 150 ms")
    check(held_right["gba"].frame_masks == [0] * 100 and
          len(held_right["volume_overlays"]) == 4,
          "held X is consumed and unchanged/clamped frames do not redraw the OSD")

    held_left = run_loop(False, frames=220, input_events=lambda frame, ms:
                         control_event(-1, k=frame == 0))
    check(not held_left["errors"] and held_left["audio"][0].set_volume_calls ==
          list(range(75, -1, -5)),
          "holding X left reaches zero and stops issuing clamped volume calls")
    check(all(visible for _value, visible, _frame, _ms in held_left["volume_overlays"]),
          "a held joystick refreshes activity and keeps the volume window open")

    reset_events = [control_event(1, True), control_event(), control_event(1),
                    control_event(-1), control_event(-1), control_event()]
    reset = run_loop(False, frames=len(reset_events), input_events=reset_events)
    check(not reset["errors"] and reset["audio"][0].set_volume_calls == [85, 90, 85],
          "returning to neutral rearms an immediate step and reversing responds immediately")
    check(reset["audio"][0].volume_call_times[-1] -
          reset["audio"][0].volume_call_times[-2] < 18,
          "direction reversal responds within one frame without the repeat delay")

    timeout_events = [control_event(k=True)] + [control_event()] * 169 + [control_event(1)]
    timeout = run_loop(False, frames=len(timeout_events), input_events=timeout_events)
    overlays = timeout["volume_overlays"]
    check(not timeout["errors"] and len(overlays) == 2 and
          [(value, visible) for value, visible, _frame, _ms in overlays] ==
          [(80, True), (80, False)] and 2500 <= overlays[1][3] - overlays[0][3] < 2518,
          "the volume window hides once after 2.5 seconds without activity")
    check(timeout["audio"][0].set_volume_calls == [] and
          timeout["gba"].frame_masks[-1] == (1 << 4),
          "timeout restores game direction input without changing volume")
    wrap_start = ((1 << 30) - 1000) * 1000
    timeout_wrap = run_loop(False, frames=len(timeout_events), input_events=timeout_events,
                            start_us=wrap_start, tick_period=1 << 30)
    normalized_wrap = [(v, shown, frame, round(ms - wrap_start / 1000.0, 6))
                       for v, shown, frame, ms in timeout_wrap["volume_overlays"]]
    normalized_plain = [(v, shown, frame, round(ms, 6)) for v, shown, frame, ms in overlays]
    check(not timeout_wrap["errors"] and normalized_wrap == normalized_plain and
          timeout_wrap["gba"].frame_masks == timeout["gba"].frame_masks,
          "volume timeout and restored directions remain exact across ticks_ms wrap")
    held_wrap = run_loop(False, frames=100, input_events=lambda frame, ms:
                         control_event(1, k=frame == 0),
                         start_us=wrap_start, tick_period=1 << 30)
    check(not held_wrap["errors"] and held_wrap["audio"][0].set_volume_calls ==
          held_audio.set_volume_calls and
          [round(ms - wrap_start / 1000.0, 6) for ms in held_wrap["audio"][0].volume_call_times] ==
          [round(ms, 6) for ms in times],
          "volume repeat delay and rate remain exact across ticks_ms wrap")

    silent = run_loop(False, frames=len(volume_events), input_events=volume_events,
                      audio_enabled=False)
    check(not silent["errors"] and silent["audio"] == [] and
          [(v, visible) for v, visible, _frame, _ms in silent["volume_overlays"]] ==
          [(80, True), (85, True), (80, True), (85, True), (85, False)] and
          silent["gba"].key_masks == [0, 1 << 4],
          "audio unavailable still allows volume UI, close, and restored game input")

    section("5a. real volume OSD: dirty updates and restoring six controls")
    osd_events = [control_event(), control_event(1, True), control_event(1),
                  control_event(), control_event(k=True), control_event(1)]
    volume_osd = run_loop(False, frames=len(osd_events), input_events=osd_events,
                          controls=True, real_display=True)
    osd_submissions = [item for item in volume_osd["submissions"] if
                       item[1].get("layer") == volume_osd["mod"].Display.LAYER_OSD1]
    check(not volume_osd["errors"] and [frame for _ref, _kw, frame in osd_submissions] ==
          [0, 1, 4],
          "OSD submits once at startup, once for open+step, and once for close")
    labels_by_frame = {}
    for _ref, op, args, _kw, frame in volume_osd["drawing"]:
        if op == "text":
            labels_by_frame.setdefault(frame, []).append(args[-1])
    six_labels = {"A", "B", "L", "R", "START", "SELECT"}
    check(six_labels.issubset(set(labels_by_frame.get(1, []))) and
          {"VOLUME", "85%"}.issubset(set(labels_by_frame.get(1, []))),
          "opening the real OSD preserves six buttons and shows volume 85 percent")
    check(set(labels_by_frame.get(4, [])) == six_labels and
          len(labels_by_frame.get(4, [])) == 6 and
          any(op == "clear" and frame == 4 for _ref, op, _args, _kw, frame
              in volume_osd["drawing"]),
          "closing clears the volume panel and restores exactly the six controls")
    game_ref = next(ref for args, _kw, ref in volume_osd["images"] if args[2] == 1)
    check(all(ref is not game_ref for ref, *_draw in volume_osd["drawing"]) and
          volume_osd["gba"].frame_masks[-1] == (1 << 4),
          "volume drawing never touches game RGB565 and game directions resume after close")

    section("5b. real display init: persistent transparent OSD and failures")
    overlay = run_loop(False, controls=True, real_display=True)
    check(not overlay["errors"] and overlay["gba"].frames == 120,
          "real display startup runs all 120 game frames without exceptions")
    check(overlay["init_calls"][0][1].get("osd_num") == 2 and
          overlay["native_buffer_bytes"] == 800 * 480 * 4,
          "two OSDs initialize and first submission sizes full ARGB storage")
    submitted = overlay["submissions"]
    check(len(submitted) == 121 and submitted[0][1] ==
          {"layer": overlay["mod"].Display.LAYER_OSD1, "alpha": 255} and
          all(kw.get("layer") == overlay["mod"].Display.LAYER_OSD0
              for _ref, kw, _frame in submitted[1:]),
          "overlay submits once on OSD1; every game frame uses OSD0")
    labels = [args[-1] for _ref, op, args, _kw, _frame in overlay["drawing"]
              if op == "text"]
    check(set(labels) == {"A", "B", "L", "R", "START", "SELECT"} and
          len(labels) == 6 and all(frame == 0 for *_draw, frame in overlay["drawing"]),
          "six visible controls are drawn once before any emulated frame")
    game_ref = next(ref for args, _kw, ref in overlay["images"] if args[2] == 1)
    check(all(ref is not game_ref for ref, *_draw in overlay["drawing"]) and
          overlay["overlay_liveness"] == [[True]] * 120,
          "game pixels receive no button drawing and overlay survives every frame")
    check(overlay["release_snapshots"] == [[True, False]] and
          overlay["lifecycle"][-2:] == ["display deinit", "media deinit"] and
          overlay["mod"]._display_overlay is None and
          all(ref() is None for _args, _kw, ref in overlay["images"]),
          "overlay stays alive through Display.deinit then all images release")
    hidden = run_loop(False, controls=False, real_display=True)
    check(not hidden["errors"] and hidden["drawing"] and
          all(op == "clear" for _ref, op, *_draw in hidden["drawing"]) and
          hidden["native_buffer_bytes"] == 800 * 480 * 4,
          "hidden-controls mode still primes full transparent ARGB storage")
    for failure in ("overlay allocation", "overlay draw", "overlay show"):
        failed = run_loop(False, controls=True, real_display=True,
                          startup_failure=failure)
        check(failed["gba"].frames == 0 and failed["gba"].unloads == 1 and
              failed["gba"].saved_paths == [] and failed["inputs"][0].deinits == 1 and
              failed["mod"]._display_overlay is None and
              failed["lifecycle"][-2:] == ["display deinit", "media deinit"] and
              all(ref() is None for _args, _kw, ref in failed["images"]),
              "%s failure releases display/input/core without rewriting save" % failure)


def main():
    print("board Python logic tests (host-side, with fakes)")
    print("=" * 76)
    test_input()
    test_touch()
    test_layout_consistency()
    test_async_touch()
    test_joystick_button()
    test_control_geometry()
    test_audio()
    test_save_guard()
    test_frame_loop_timing()

    print("\n" + "=" * 76)
    print("checks: %d, failures: %d" % (len(checks), len(failures)))
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  !! " + f)
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
