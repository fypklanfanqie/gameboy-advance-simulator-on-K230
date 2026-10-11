#!/usr/bin/env python3
"""Static sanity checks on the board-side Python before deploying it.

Catches the classes of mistake that previously wasted board round-trips:
  * syntax errors,
  * the audio stream being created before MediaManager.init() (PyAudio's
    Write_stream allocates a VB pool in start_stream, so it must come after),
  * the touch API being used as a tuple (the vendor TOUCH.read returns objects),
  * image.Image being constructed without the pixel-format argument,
  * use of the retired `input` module name (shadows nothing useful, but the old
    one used tuple unpacking and the wrong ADC range),
  * ADC read_u16 error sentinel (65535) not being filtered.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

problems = []
notes = []


def read(name):
    return (SRC / name).read_text(encoding="utf-8")


# ---- 1. syntax ----
for name in ("main.py", "gba_ui.py", "gba_input.py", "gba_audio.py"):
    try:
        compile(read(name), name, "exec")
        notes.append("%s: syntax OK" % name)
    except SyntaxError as exc:
        problems.append("%s: SyntaxError %s" % (name, exc))

main = read("main.py")
inp = read("gba_input.py")
aud = read("gba_audio.py")

# ---- 2. audio after MediaManager.init ----
i_disp = main.find('init_display("[emu]"')
i_audio = main.find("import gba_audio")
if i_disp == -1 or i_audio == -1:
    problems.append("main.py: could not locate the display/audio init sites")
elif i_audio < i_disp:
    problems.append("main.py: audio is initialized BEFORE MediaManager.init() "
                    "(PyAudio allocates a VB pool in start_stream)")
else:
    notes.append("main.py: audio init comes after display/MediaManager.init OK")

# ---- 3. blip rate must be set explicitly ----
if "set_audio_rate" not in main:
    problems.append("main.py: never calls gba.set_audio_rate() - the firmware blip "
                    "rate defaults to 96000 while PyAudio would run at a different rate")
else:
    notes.append("main.py: calls gba.set_audio_rate OK")
m = re.search(r"AUDIO_RATE\s*=\s*(\d+)", main)
if m:
    notes.append("main.py: AUDIO_RATE=%s" % m.group(1))
else:
    problems.append("main.py: AUDIO_RATE not defined")

# ---- 4. touch API must use attributes, not tuple unpacking ----
if re.search(r"for\s+\w+\s+in\s+.*tp\.read\(\)", inp) and "p.x" not in inp:
    problems.append("gba_input.py: touch points look like they are unpacked as tuples; "
                    "the vendor TOUCH.read() returns TOUCH_INFO objects (use .x/.y/.event)")
bad_unpack = re.search(r"px\s*,\s*py\s*,\s*event\s*=\s*p\b", inp)
if bad_unpack:
    problems.append("gba_input.py: still unpacks a touch point as a 3-tuple: %r"
                    % bad_unpack.group(0))
if ".event" in inp and "EVENT_UP" in inp:
    notes.append("gba_input.py: uses .event and filters EVENT_UP OK")
else:
    problems.append("gba_input.py: does not filter the touch release event")

# ---- 5. ADC error sentinel ----
if "65535" in inp or "0xFFFF" in inp or "ADC_ERR" in inp:
    notes.append("gba_input.py: has an ADC error sentinel OK")
else:
    problems.append("gba_input.py: no ADC error sentinel; drv_adc_read returns "
                    "__UINT32_MAX__ on failure which read_u16() masks to 65535")

# ---- 6. image.Image must carry a pixel format ----
for ctor in re.findall(r"_image\.Image\([^)]*\)", main):
    if "RGB565" not in ctor and "ARGB8888" not in ctor and "RGB888" not in ctor:
        problems.append("main.py: image.Image without a pixel format: %s" % ctor)
notes.append("main.py: image.Image constructor(s) carry a pixel format OK")

# ---- 7. zero-copy display contract ----
if "alloc=4" in main:
    notes.append("main.py: uses alloc=4 (ALLOC_REF) for the zero-copy path OK")
else:
    problems.append("main.py: does not use alloc=4 for the zero-copy display path")
if "disp_bytes" in main:
    notes.append("main.py: wraps gba.disp_bytes() OK")
else:
    problems.append("main.py: never calls gba.disp_bytes()")

# ---- 8. release contract ----
if main.find("Display.deinit()") < main.find("MediaManager.deinit()"):
    notes.append("main.py: release order Display.deinit -> MediaManager.deinit OK")
else:
    problems.append("main.py: release order is wrong (must be Display.deinit then "
                    "exitpoint/sleep then MediaManager.deinit)")

# ---- 9. no stub shadowing ON THE DEPLOY LIST ----
# src/gba.py, gba_fixed.py, gba_minimal.py are historical Python stubs of the
# emulator. They must never be copied to the card: a Python gba.py on /sdcard
# shadows the built-in C module, so `import gba` would silently get the stub
# instead of the real emulator. The deploy list is the contract that matters.
deploy = (ROOT / "tools" / "deploy_gba_card.py").read_text(encoding="utf-8")
deployed_names = set(re.findall(r'SRC\s*/\s*"([^"]+)"', deploy))
stubs = {"gba.py", "gba_fixed.py", "gba_minimal.py"}
shadow = stubs & deployed_names
if shadow:
    problems.append("deploy_gba_card.py would deploy stub module(s) %s, which shadow "
                    "the built-in C gba module" % sorted(shadow))
else:
    notes.append("deploy list excludes the gba.py stub family OK")
for name in sorted(deployed_names):
    if not (SRC / name).is_file():
        problems.append("deploy list references missing src/%s" % name)
notes.append("deploy list: %s" % ", ".join(sorted(deployed_names)))

# ---- 10. buttons and touch must share ONE region table ----
# main.py draws the on-screen buttons; gba_input.py decides which touches count
# as which button. If the two keep separate copies (or disagree on LAYOUT) the
# visible button and the pressable area drift apart - silently. Both must read
# src/gba_ui.py, so a mismatch becomes structurally impossible.
ui_path = SRC / "gba_ui.py"
if not ui_path.is_file():
    problems.append("src/gba_ui.py is missing - it is the single source of truth "
                    "for layout and button regions")
else:
    ui = ui_path.read_text(encoding="utf-8")
    if "BUTTON_REGIONS" in ui and "LAYOUT_SCALE" in ui:
        notes.append("gba_ui.py: defines BUTTON_REGIONS + LAYOUT_SCALE OK")
    else:
        problems.append("gba_ui.py: missing BUTTON_REGIONS or LAYOUT_SCALE")
    for mod, name in ((main, "main.py"), (inp, "gba_input.py")):
        if "from gba_ui import" in mod or "import gba_ui" in mod:
            notes.append("%s: reads layout/regions from gba_ui OK" % name)
        else:
            problems.append("%s: does not import gba_ui, so its button/touch table "
                            "can drift out of sync" % name)
        # A duplicated literal region table would defeat the shared source.
        if re.search(r'"Select"\s*,\s*\d+\s*,\s*\d+', mod):
            problems.append("%s: still contains a hardcoded button region table; "
                            "it must come from gba_ui.py" % name)

# ---- 11. saves must not be snapshotted while the game is mid-write ----
# In this build the core's savedata has no VFile, so GBASavedataClean() is a
# no-op and NOTHING flushes automatically; the Python layer does. If it writes
# while a FLASH sector erase/program is still settling, savedata.data is in a
# half-written state and the .sav would capture garbage over a good save.
if "save_busy" in main:
    notes.append("main.py: checks gba.save_busy() before saving OK")
else:
    problems.append("main.py: never checks gba.save_busy() - a periodic save can "
                    "capture savedata.data mid-FLASH-write and corrupt the .sav")
if "def save_if_idle" in main:
    notes.append("main.py: routes saves through save_if_idle() OK")
else:
    problems.append("main.py: no save_if_idle() helper; raw save_write() calls can "
                    "race the emulated flash write")
# A bare save_write outside the helper would bypass the busy check.
bare = [m for m in re.finditer(r"gba\.save_write\(", main)]
notes.append("main.py: gba.save_write call sites: %d" % len(bare))

# ---- 12. frame pacing: the emulator does not self-throttle ----
# gba.run_frame() has no vsync, so the loop runs as fast as the CPU allows.
# Without explicit pacing the game plays fast (music/timers wrong) and audio is
# produced faster than it can be played, which fills the core's blip buffer.
if "TARGET_FPS" in main and "time.sleep_ms(slack)" in main:
    notes.append("main.py: paces the main loop to TARGET_FPS OK")
else:
    problems.append("main.py: no frame pacing - gba.run_frame() does not throttle, "
                    "so the game would run fast and audio would over-produce")

# ---- 13. audio writes must be whole blocks ----
# pyaudio.Write_stream.write() does
#     uctypes.bytearray_at(virt, frame.len)[:] = data   # zero-pads if short
#     kd_mpi_ao_send_frame(...)
# so a SHORT write still plays a full block (padded with silence) - the play
# time is always frame.len, regardless of how many bytes were passed. Sending
# a partial block therefore causes underruns. The module must accumulate.
if "block_bytes" in aud and "while len(self._accum) >= self.block_bytes" in aud:
    notes.append("gba_audio.py: accumulates whole blocks before writing OK")
else:
    problems.append("gba_audio.py: does not accumulate whole audio blocks; a short "
                    "write is zero-padded to frame.len and plays the full block")
if "frames_per_buffer" in aud and "block_bytes" in aud:
    notes.append("gba_audio.py: block size derives from frames_per_buffer OK")
else:
    problems.append("gba_audio.py: audio block size is not tied to frames_per_buffer")
# GBA produces ~738 samples/frame at 44100 Hz; the block must not be smaller
# than that or we would write more often than production (still fine) - but it
# must not be so large that latency suffers. Report the chosen value.
m = re.search(r"DEFAULT_FRAMES_PER_BUFFER\s*=\s*(\d+)", aud)
if m:
    n = int(m.group(1))
    notes.append("gba_audio.py: block=%d samples (~%.1f ms at 44100)"
                 % (n, n * 1000.0 / 44100))
    if n < 738:
        problems.append("gba_audio.py: block of %d samples is smaller than the ~738 "
                        "samples the GBA produces per frame" % n)

# ---- 14. the display wrapper MUST use the fixed 720x480 stride ----
# Show_image takes its stride from image->w (modmedia.display.c:
# buffer->vf_info.v_frame.stride[0] = image->w * bpp), while the C scaler always
# writes a 720-pixel-wide row. Wrapping the same buffer at a different width
# therefore shears the picture - and it fails SILENTLY, because
# py_image_alloc only checks `size > bufinfo.len` (480*320*2 = 307200 is less
# than the 691200-byte buffer, so the check passes).
if "DISP_W, DISP_H = 720, 480" in (main + (ui if ui_path.is_file() else "")):
    notes.append("display buffer is declared 720x480 OK")
else:
    problems.append("the 720x480 display buffer constant is missing")

bad_wrap = re.findall(r"_image\.Image\(\s*([A-Za-z_0-9]+)\s*,\s*([A-Za-z_0-9]+)\s*,"
                      r"\s*_image\.RGB565", main)
for w, h in bad_wrap:
    if (w, h) != ("DISP_W", "DISP_H"):
        problems.append("main.py: image.Image wrapping the scale buffer uses (%s,%s); "
                        "it must be (DISP_W, DISP_H) = (720,480) or the picture is "
                        "sheared by a stride mismatch" % (w, h))
if bad_wrap and all(wh == ("DISP_W", "DISP_H") for wh in bad_wrap):
    notes.append("main.py: all scale-buffer wraps use DISP_W/DISP_H OK")

# Layout changes must go through the firmware scaler, not a different width.
if "scale_into" in main:
    notes.append("main.py: layouts are applied via gba.scale_into() OK")
else:
    problems.append("main.py: does not call gba.scale_into(); a layout change would "
                    "have to resize the image and break the stride")
if "display_scale()" in main:
    problems.append("main.py: still calls the fixed-3x gba.display_scale(); use "
                    "gba.scale_into(scale, off_x, off_y) so LAYOUT is honoured")

print("=== notes ===")
for n in notes:
    print("  " + n)
print()
if problems:
    print("=== PROBLEMS (%d) ===" % len(problems))
    for p in problems:
        print("  !! " + p)
    sys.exit(1)
print("All static checks passed.")
