#!/usr/bin/env python3
"""Apply literal text replacements to files inside the WSL vendor tree.

Why this exists: Windows-side editors (including the agent's edit tool) fail on
\\\\wsl.localhost paths with "GetFileSecurityW EIO", and inlining C source into a
`wsl bash -c "..."` command mangles quotes, $ and backslashes. This script is
copied into WSL and executed there, so replacement text never passes through a
shell.

Usage (from Windows):
    python tools/wslpatch.py <patchfile.json>

patchfile.json = list of {"file": abs_or_rel_posix_path, "old": "...", "new": "...",
                          "count": 1 (optional, default 1)}
Paths are relative to an explicit second root argument or SDK_PATH.
"""
import json
import sys
import os

ROOT = os.environ.get("SDK_PATH")


def main():
    if len(sys.argv) < 2:
        print("usage: wslpatch.py <patchfile.json> [root]")
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        patches = json.load(fh)
    if len(sys.argv) > 2:
        root = sys.argv[2]
    else:
        root = ROOT
    if root is None:
        print("ERROR: explicit root argument or SDK_PATH is required")
        return 2

    failures = 0
    for i, p in enumerate(patches):
        path = p["file"]
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        try:
            with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
                data = fh.read()
        except OSError as exc:
            print("[%d] FAIL read %s: %s" % (i, path, exc))
            failures += 1
            continue

        if "regex" in p:
            # Regex mode: whitespace-tolerant structural edit. The pattern must
            # match exactly `expect` times, so an unintended second match aborts.
            expect = p.get("count", 1)
            import re
            rx = re.compile(p["regex"])
            hits = rx.findall(data)
            if len(hits) != expect:
                print("[%d] FAIL %s: regex matched %d time(s), expected %d"
                      % (i, path, len(hits), expect))
                failures += 1
                continue
            data = rx.sub(p["new"], data, expect)
        elif "line_start" in p:
            # Line-range mode: replace 1-based inclusive [line_start, line_end]
            # with new_lines. Verify the existing lines match old_lines when given,
            # so a stale line number can never silently corrupt a different block.
            lines = data.split("\n")
            start = p["line_start"] - 1
            end = p["line_end"]          # exclusive slice index
            if start < 0 or end > len(lines) or start >= end:
                print("[%d] FAIL %s: line range %d-%d out of bounds (%d lines)"
                      % (i, path, p["line_start"], p["line_end"], len(lines)))
                failures += 1
                continue
            actual = lines[start:end]
            if "old_lines" in p and actual != p["old_lines"]:
                print("[%d] FAIL %s: content at lines %d-%d does not match old_lines"
                      % (i, path, p["line_start"], p["line_end"]))
                for n, (got, want) in enumerate(zip(actual, p["old_lines"])):
                    if got != want:
                        print("    line %d:\n      got  %r\n      want %r"
                              % (p["line_start"] + n, got, want))
                failures += 1
                continue
            lines[start:end] = p["new_lines"]
            data = "\n".join(lines)
        else:
            old = p["old"]
            new = p["new"]
            expect = p.get("count", 1)
            found = data.count(old)
            if found != expect:
                print("[%d] FAIL %s: expected %d occurrence(s) of old, found %d"
                      % (i, path, expect, found))
                failures += 1
                continue
            data = data.replace(old, new, expect)
        tmp = path + ".wslpatch.tmp"
        try:
            with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError as exc:
            print("[%d] FAIL write %s: %s" % (i, path, exc))
            failures += 1
            continue
        print("[%d] OK  %s (%d replacement(s))" % (i, os.path.relpath(path, root), expect))
    print("done: %d patch(es), %d failure(s)" % (len(patches), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
