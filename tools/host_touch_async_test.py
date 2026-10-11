#!/usr/bin/env python3
"""Gate the native cached touch worker under ASan/UBSan using a blocking driver."""
import subprocess
import sys
import tempfile
from pathlib import Path

from install_touch_async import SOURCE, patched_source
from native_tools import SKIP, run_native

ROOT = Path(__file__).resolve().parents[1]

def test_installer_drift_guard():
    # Exercise real installation on an isolated fake SDK, never on the card.
    with tempfile.TemporaryDirectory(prefix="pokemo-touch-test-") as tmp:
        sdk = Path(tmp)
        dst = sdk / "src/canmv/port/machine"
        dst.mkdir(parents=True)
        vendor = (SOURCE / "machine_touch.vendor.c").read_bytes()
        (dst / "machine_touch.c").write_bytes(vendor)
        cmd = [sys.executable, str(ROOT / "tools/install_touch_async.py"), "--sdk", str(sdk)]
        first = subprocess.run(cmd, capture_output=True, text=True)
        if first.returncode != 0:
            raise AssertionError(first.stderr)
        if (dst / "machine_touch.c.before-async").read_bytes() != vendor:
            raise AssertionError("vendor backup changed")
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            raise AssertionError("idempotent install failed")
        for name in ("machine_touch.c", "touch_async.c", "touch_async.h"):
            target = dst / name
            prior = target.read_bytes()
            drift = prior + b"\n/* test drift */\n"
            target.write_bytes(drift)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 or "Installed source drift" not in result.stderr:
                raise AssertionError("failed to refuse drift in " + name)
            if target.read_bytes() != drift:
                raise AssertionError("drift refusal wrote source")
            target.write_bytes(prior)
    print("installer drift guard: idempotency, backup, and all 3 refusal paths passed")

def main():
    # The installer is deterministic and guards its exact vendor baseline.
    if (SOURCE / "machine_touch.c").read_text() != patched_source():
        print("FAIL: machine_touch.c differs from reproducible installer output")
        return 1
    test_installer_drift_guard()
    code, output = run_native("test_touch_async.sh")
    for line in output.splitlines():
        if "localhost" not in line and "\0" not in line:
            print(line)
    if code == SKIP:
        print("SKIP: native touch test unavailable; installer drift guard passed")
        return SKIP
    if code != 0 or "159/159 checks passed" not in output:
        print("RESULT: FAIL native cached touch worker")
        return 1
    print("RESULT: PASS native cached touch worker (159 checks, ASan/UBSan, drift guard)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
