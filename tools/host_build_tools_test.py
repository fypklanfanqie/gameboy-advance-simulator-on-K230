#!/usr/bin/env python3
"""Test portable build installers and pin refusal in isolated fake SDKs."""
from pathlib import Path
import json
import os
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
        sys.path.insert(0, str(ROOT / 'tools'))
        from install_gba_perf import BLOCK, OLD_BLOCK, PREVIOUS_BLOCK, RTC_BLOCK
        makefile = port / 'Makefile'
        assert '-DHAVE_LOCALTIME_R=1' in makefile.read_text()
        probe = port / 'gba/core/arm/flag_probe.c'
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('/* rebuild probe */\n')
        for previous in (OLD_BLOCK, PREVIOUS_BLOCK, RTC_BLOCK):
            makefile.write_text(previous + 'include $(TOP)/py/mkrules.mk\n')
            os.utime(probe, (1, 1))
            migrated = subprocess.run(command, capture_output=True, text=True)
            assert migrated.returncode == 0, migrated.stderr
            assert makefile.read_text().startswith(BLOCK)
            assert 'hle-bios.o: gba/core/gba/hle-bios.c' in makefile.read_text()
            assert probe.stat().st_mtime > 1
            manifest = json.loads((port / 'gba/perf-flags-installed.json').read_text())
            assert manifest['feature_defines'] == ['HAVE_LOCALTIME_R=1']
            checks += 4
        drift_flags = BLOCK.replace('-O3', '-O2')
        makefile.write_text(drift_flags)
        assert subprocess.run(command, capture_output=True).returncode != 0
        assert makefile.read_text() == drift_flags
        checks += 2
        makefile.write_text(BLOCK + 'include $(TOP)/py/mkrules.mk\n')
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
