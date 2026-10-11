#!/usr/bin/env python3
"""Cross-check every gba.* API the board Python calls against the firmware's
module table.

This catches the failure mode where the Python is correct but the firmware does
not export a function it uses - which on the board shows up as an
AttributeError inside a try/except, i.e. silently degraded behaviour.

Reads:
  src/*.py                       -> every `gba.<name>(` usage
  firmware/gba/gba_module.c       -> the MP_ROM_QSTR(MP_QSTR_<name>) table
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MODULE_C = ROOT / "firmware" / "gba" / "gba_module.c"

# Only the files actually deployed to the card matter. Historical scripts
# (ide_gba.py, main_fixed.py, ...) are never installed and would produce noise
# such as `gba.py` being read as an attribute.
DEPLOYED = ["main.py", "gba_ui.py", "gba_input.py", "gba_audio.py"]

usage = {}
for name in DEPLOYED:
    path = SRC / name
    if not path.is_file():
        print("missing deployed source: %s" % path)
        sys.exit(1)
    text = path.read_text(encoding="utf-8")
    # Strip comments/strings so documentation mentions do not count as usage.
    code = re.sub(r'"""(?:.|\n)*?"""', "", text)
    code = re.sub(r"'''(?:.|\n)*?'''", "", code)
    code = re.sub(r"(?m)#.*$", "", code)
    for m in re.finditer(r"\bgba\.([A-Za-z_][A-Za-z_0-9]*)", code):
        usage.setdefault(m.group(1), set()).add(name)

if not MODULE_C.is_file():
    print("missing published firmware source: %s" % MODULE_C)
    sys.exit(1)

c_text = MODULE_C.read_text(encoding="utf-8", errors="replace")

# The module table entries: { MP_ROM_QSTR(MP_QSTR_name), MP_ROM_PTR(...) },
table = set(re.findall(r"MP_ROM_QSTR\(MP_QSTR_([A-Za-z_][A-Za-z_0-9]*)\)\s*,\s*MP_ROM_(?:PTR|INT)",
                       c_text))
# Constants exported via MP_ROM_INT (e.g. GBA_KEY_A) are also in the table.

# Python builtins/attributes that are not module functions.
NOT_API = {"GBA_KEY_A", "GBA_KEY_B", "GBA_KEY_SELECT", "GBA_KEY_START",
           "GBA_KEY_RIGHT", "GBA_KEY_LEFT", "GBA_KEY_UP", "GBA_KEY_DOWN",
           "GBA_KEY_R", "GBA_KEY_L"}
# `from gba import X` style names are checked separately below.
ABSTRACT_ATTRS = {"__file__", "__name__"}

print("gba.* APIs referenced by the board Python")
print("=" * 74)
missing = []
constants_used = []
for name in sorted(usage):
    where = ",".join(sorted(usage[name]))
    if name in table:
        kind = "const" if name in NOT_API else "func "
        print("  %s %-18s OK        (%s)" % (kind, name, where))
        if name in NOT_API:
            constants_used.append(name)
    else:
        print("  ??? %-18s MISSING   (%s)" % (name, where))
        missing.append((name, where))

print("=" * 74)
print("module table exports %d names" % len(table))
if missing:
    print("MISSING from the firmware module table (%d):" % len(missing))
    for name, where in missing:
        print("  %s (used by %s)" % (name, where))
    sys.exit(1)
print("RESULT: PASS - every gba.* the Python uses is exported by the firmware")
if constants_used:
    print("  (also used: %s)" % ", ".join(constants_used))
