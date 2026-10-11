#!/usr/bin/env bash
# Build the user-space CanMV application; never write firmware or a card.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${SDK_PATH:?Set SDK_PATH to an existing SDK checkout prepared using official instructions}"
SDK="$(cd -- "$SDK_PATH" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
OUT="$ROOT/build/firmware"
mkdir -p "$OUT"
"$PYTHON_BIN" "$ROOT/tools/check_source_pins.py" --sdk "$SDK"
"$PYTHON_BIN" "$ROOT/tools/check_firmware_manifest.py"
PATCH="$ROOT/firmware/patches/canmv-integration.patch"
if git -C "$SDK/src/canmv" apply --check "$PATCH"; then
  git -C "$SDK/src/canmv" apply "$PATCH"
elif ! git -C "$SDK/src/canmv" apply --reverse --check "$PATCH"; then
  echo "FAIL: integration patch does not match the checked SDK" >&2
  exit 1
fi
mkdir -p "$SDK/src/canmv/port/gba"
cp -R "$ROOT/firmware/gba/." "$SDK/src/canmv/port/gba/"
# Keep installed sources newer than old objects, including on repeated builds.
find "$SDK/src/canmv/port/gba" -name '*.c' -exec touch {} +
"$PYTHON_BIN" "$ROOT/tools/install_touch_async.py" --sdk "$SDK"
"$PYTHON_BIN" "$ROOT/tools/install_gba_perf.py" --sdk "$SDK"
make -C "$SDK/src/canmv" build NCPUS="${NCPUS:-4}" >"$OUT/build.log" 2>&1 || {
  tail -60 "$OUT/build.log"
  exit 1
}
CONFIG="${DEFCONFIG:-k230_canmv_hiwonder_defconfig}"
"$PYTHON_BIN" "$ROOT/tools/rtapp_tool.py" generate --sdk "$SDK" --defconfig "$CONFIG"
BUILD="$SDK/output/$CONFIG"
cp "$BUILD/canmv/micropython" "$OUT/micropython"
cp "$BUILD/images/rtapp/rtapp.elf.gz" "$OUT/rtapp.elf.gz"
cp "$BUILD/images/rtapp/rtapp.elf.gz.verify.txt" "$OUT/rtapp.elf.gz.verify.txt"
"$PYTHON_BIN" "$ROOT/tools/rtapp_tool.py" verify "$OUT/rtapp.elf.gz" --expected "$OUT/micropython"
echo "BUILD PASS: artifacts in $OUT; card and raw slots were not written"
