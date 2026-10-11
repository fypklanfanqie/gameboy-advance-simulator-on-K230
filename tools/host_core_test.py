"""Run the Emerald-specific mGBA lifecycle harness with an external ROM.

ROM data stays outside the repository. With no --rom or GBA_TEST_ROM this test
returns SKIP (77). The harness currently expects BPEE and 128 KiB FLASH saves.
"""
import argparse
import os
from pathlib import Path
import sys
from native_tools import SKIP, native_path, run_native

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, default=os.environ.get("GBA_TEST_ROM"))
    parser.add_argument("--frames", default="60,120,300,900")
    args = parser.parse_args()
    if args.rom is None:
        print("SKIP: core runtime test needs an external ROM (--rom or GBA_TEST_ROM)")
        return SKIP
    if not args.rom.is_file():
        parser.error("ROM does not exist: " + str(args.rom))
    with args.rom.open("rb") as stream:
        stream.seek(0xac)
        game_code = stream.read(4)
    if game_code != b"BPEE":
        parser.error("this lifecycle harness expects an externally supplied BPEE ROM")
    try:
        budgets = [int(value) for value in args.frames.split(",")]
        if not budgets or min(budgets) <= 0:
            raise ValueError
    except ValueError:
        parser.error("--frames must be positive comma-separated integers")
    try:
        rom_path = native_path(args.rom)
    except RuntimeError as exc:
        print("SKIP: " + str(exc))
        return SKIP
    code, output = run_native("hosttest_core.sh", [rom_path, args.frames])
    print(output, end="" if output.endswith("\n") else "\n")
    return code

if __name__ == "__main__":
    sys.exit(main())
