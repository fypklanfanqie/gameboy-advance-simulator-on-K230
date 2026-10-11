#!/usr/bin/env python3
"""Run portable source gates, optionally adding external-ROM and card checks.

No board, card, SDK, or ROM is needed for the default source tests. Native tests
need Linux (or Windows WSL), bash, gcc/clang and sanitizer runtimes. A missing
native environment is explicitly SKIP (77), never PASS. --rom adds the
Emerald-specific lifecycle harness; --drive only reads card runtime files.
"""
import argparse
from pathlib import Path
import subprocess
import sys
from deploy_gba_card import FILES, card_root, sha256_file

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, help="external BPEE ROM for optional core test")
    parser.add_argument("--drive", help="mounted card directory for read-only script comparison")
    args = parser.parse_args()
    gates = [("Python logic", "host_py_logic_test.py"),
             ("Published source manifest", "check_firmware_manifest.py"),
             ("Deployment protections", "host_deploy_test.py"),
             ("Build tooling protections", "host_build_tools_test.py"),
             ("Audio probe logic", "host_audio_probe_test.py"),
             ("Board static checks", "check_board_py.py"),
             ("Undefined names", "check_undefined_names.py"),
             ("Checker self-test", "selftest_undefined_check.py"),
             ("Firmware API exports", "check_gba_api.py"),
             ("RTAPP parser", "host_rtapp_test.py"),
             ("Native touch / drift guard", "host_touch_async_test.py"),
             ("Pixel-exact scaler", "host_scale_test.py")]
    results = []
    for label, script in gates:
        result = subprocess.run([sys.executable, str(ROOT / "tools" / script)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = (result.stdout or "") + (result.stderr or "")
        status = "PASS" if result.returncode == 0 else ("SKIP" if result.returncode == 77 else "FAIL")
        lines = output.strip().splitlines()
        print("%-32s %-4s %s" % (label, status, lines[-1] if lines else "no output"))
        if status == "FAIL":
            print(output)
        results.append((label, status))
    if args.rom is not None:
        result = subprocess.run([sys.executable, str(ROOT / "tools/host_core_test.py"), "--rom", str(args.rom)], cwd=ROOT)
        results.append(("External ROM core", "PASS" if result.returncode == 0 else ("SKIP" if result.returncode == 77 else "FAIL")))
    else:
        print("External ROM core                SKIP no --rom supplied")
        results.append(("External ROM core", "SKIP"))
    if args.drive is not None:
        root = card_root(args.drive)
        bad = []
        for source, name in FILES:
            target = root / name
            if not target.is_file() or sha256_file(target) != sha256_file(source):
                bad.append(name)
        status = "FAIL" if bad else "PASS"
        print("Card scripts                     %s %s" % (status, ", ".join(bad) if bad else "all four byte-identical"))
        results.append(("Card scripts", status))
    else:
        print("Card scripts                     SKIP no --drive supplied")
        results.append(("Card scripts", "SKIP"))
    failed = [label for label, status in results if status == "FAIL"]
    skipped = [label for label, status in results if status == "SKIP"]
    print("OVERALL: %s; %d passed, %d skipped, %d failed" %
          ("FAIL" if failed else "PASS (executed checks)",
           sum(status == "PASS" for _, status in results), len(skipped), len(failed)))
    if skipped:
        print("SKIP: " + ", ".join(skipped))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
