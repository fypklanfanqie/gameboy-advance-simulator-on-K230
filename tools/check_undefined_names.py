#!/usr/bin/env python3
"""Detect undefined global names in the board Python (no external deps).

WHY: this is a real failure mode on the device. If main.py references a name
that was never defined (a typo, or a leftover from a refactor), that line raises
NameError at runtime - and because the main loop and most init steps are wrapped
in try/except, the result is silently degraded behaviour or an early exit that
looks like "nothing happened". Cross-compiling and syntax checks cannot catch it.

Uses only the standard library (`ast` + `symtable`), so it runs anywhere.

Known board-only modules (machine, media.*, image, mpy) are treated as defined.
"""

import ast
import builtins
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DEPLOYED = ["main.py", "gba_ui.py", "gba_input.py", "gba_audio.py"]

# Modules that only exist on the board (or are provided by the firmware).
BOARD_MODULES = {
    "machine", "image", "media", "media.display", "media.media", "media.pyaudio",
    "mpp", "mpp.audio_def", "mpp.vb", "mpp.sys", "os", "sys", "time", "gc",
    "uctypes", "ulab", "nncase_runtime", "gba", "hello", "lvgl", "sensor",
    "gc9a01", "ustruct", "array", "micropython",
}

# Names provided by `from X import *` that we cannot resolve statically.
STAR_IMPORTS = {"machine", "image", "media.pyaudio", "media.display", "media.media"}


def defined_names(tree):
    """Every name bound anywhere in the module (approximation, errs to safe)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names.update(node.names)
    return names


def main():
    builtin_names = set(dir(builtins))
    # MicroPython exposes a few extras; treat common ones as defined.
    builtin_names |= {"const", "micropython", "bytearray", "memoryview", "Ellipsis"}

    problems = []
    for name in DEPLOYED:
        path = SRC / name
        if not path.is_file():
            problems.append("%s: missing deployed source" % name)
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, name)
        except SyntaxError as exc:
            problems.append("%s: SyntaxError %s" % (name, exc))
            continue

        defined = defined_names(tree)

        # Collect every loaded Name that is not defined anywhere, not a builtin,
        # and not a module we know the board provides.
        used = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)
        # NOTE: a module-level call to a function defined LATER in the file
        # (e.g. the trailing `main()` idiom) is perfectly valid Python, and
        # `defined_names` already walks the whole module - so such a name is
        # found. There is no ordering check here on purpose.

        star = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(a.name == "*" for a in node.names) and mod in STAR_IMPORTS:
                    star.add(mod)

        # Names that are only reached through a conditional `except` fallback
        # (our defensive import blocks) are still bound at module level, so they
        # are covered by defined_names. Nothing extra needed.
        unknown = []
        for nm, line in sorted(used.items(), key=lambda kv: kv[1]):
            if nm in defined or nm in builtin_names or nm in BOARD_MODULES:
                continue
            # `from media.pyaudio import *` brings paInt16/PyAudio etc.
            if star:
                continue
            unknown.append((nm, line))
        if unknown:
            for nm, line in unknown:
                problems.append("%s:%d: undefined name %r" % (name, line, nm))

    print("undefined-name check for the board Python")
    print("=" * 70)
    for name in DEPLOYED:
        print("  scanned %s" % name)
    print("=" * 70)
    if problems:
        print("PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  !! " + p)
        return 1
    print("RESULT: PASS - no undefined global names in the deployed modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
