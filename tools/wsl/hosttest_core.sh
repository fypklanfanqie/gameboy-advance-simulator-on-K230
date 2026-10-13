#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CC_BIN="${CC:-cc}"
if ! command -v "$CC_BIN" >/dev/null 2>&1; then
  echo "SKIP: C compiler missing (install gcc/clang or set CC)"
  exit 77
fi
ROM="${1:?external ROM path required}"
BUDGETS="${2:-60,120,300,900}"
G="$ROOT/firmware/gba"
OUT="$ROOT/build/host-tests"
mkdir -p "$OUT"
mapfile -d '' SOURCES < <(find "$G/core" -name '*.c' \
  ! -path '*/third-party/inih/src/examples/*' \
  ! -path '*/third-party/inih/src/tests/*' \
  ! -path '*/third-party/inih/src/fuzzing/*' \
  ! -path '*/third-party/inih/ini.c' -print0)
"$CC_BIN" -O1 -g -std=gnu99 -D_GNU_SOURCE -DCOLOR_16_BIT -DCOLOR_5_6_5 -DHAVE_LOCALTIME_R=1 \
  -I"$G/include/include" -I"$G/core" -ffunction-sections -fdata-sections \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare -Wno-unused-function \
  -Wno-missing-field-initializers -Wl,--gc-sections \
  -o "$OUT/host_gba_test" "$ROOT/tools/hosttest/host_gba_test.c" "${SOURCES[@]}" -lm \
  >"$OUT/core-build.log" 2>&1 || { cat "$OUT/core-build.log"; exit 1; }
echo "Core compile PASS; diagnostics: $OUT/core-build.log"
IFS=',' read -ra FRAMES <<< "$BUDGETS"
for frames in "${FRAMES[@]}"; do
  log="$OUT/core-$frames.log"
  set +e
  TZ=UTC ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=0 \
    "$OUT/host_gba_test" "$ROM" "$frames" >"$log" 2>&1
  rc=$?
  set -e
  grep -E 'checks:|RESULT:' "$log" || true
  if [ "$rc" -ne 0 ] || grep -q 'ERROR: AddressSanitizer' "$log"; then
    cat "$log"
    exit 1
  fi
  if ! grep -q 'RESULT: PASS' "$log"; then cat "$log"; exit 1; fi
  ubsan=$(grep -c 'runtime error:' "$log" || true)
  echo "frames=$frames: PASS; UBSan reports=$ubsan (upstream diagnostics; inspect full log $log)"
done
echo "RESULT: PASS core lifecycle across all requested frame budgets; no ASan memory errors"
