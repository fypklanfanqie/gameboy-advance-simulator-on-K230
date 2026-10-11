#!/usr/bin/env python3
"""Test portable build installers and pin refusal in isolated fake SDKs."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    checks = 0
    with tempfile.TemporaryDirectory(prefix="k230-build-tools-") as directory:
        sdk = Path(directory)
        port = sdk / "src/canmv/port"
        port.mkdir(parents=True)
        (port / "Makefile").write_text("include $(TOP)/py/mkrules.mk\n")
        for relative in ("core/log.c", "util/gui.c", "util/string.c"):
            target = port / "gba/core" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / "firmware/gba/core" / relative).read_bytes())
        command = [sys.executable, str(ROOT / "tools/install_gba_perf.py"), "--sdk", str(sdk)]
        first = subprocess.run(command, capture_output=True, text=True)
        assert first.returncode == 0, first.stderr
        assert subprocess.run(command, capture_output=True).returncode == 0
        checks += 2
        target = port / "gba/core/core/log.c"
        drift = target.read_bytes() + b" /* drift */"
        target.write_bytes(drift)
        result = subprocess.run(command, capture_output=True)
        assert result.returncode != 0 and b"untracked changes" in result.stderr
        assert target.read_bytes() == drift
        checks += 2
        result = subprocess.run([sys.executable, str(ROOT / "tools/check_source_pins.py"),
                                 "--sdk", str(sdk)], capture_output=True)
        assert result.returncode != 0 and b"SOURCE PINS: FAIL" in result.stdout
        checks += 1
    print("Build tooling regression: %d checks, PASS" % checks)
    return 0

if __name__ == "__main__":
    sys.exit(main())
