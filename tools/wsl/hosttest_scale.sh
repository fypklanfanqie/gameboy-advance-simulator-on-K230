#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CC_BIN="${CC:-cc}"
if ! command -v "$CC_BIN" >/dev/null 2>&1; then
  echo "SKIP: C compiler missing (install gcc/clang or set CC)"
  exit 77
fi
OUT="$ROOT/build/host-tests"
mkdir -p "$OUT"
"$CC_BIN" -O1 -g -std=gnu99 -Wall -Wextra \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$ROOT/firmware/gba" -o "$OUT/host_scale_test" \
  "$ROOT/tools/hosttest/host_scale_test.c" "$ROOT/firmware/gba/gba_scale.c"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$OUT/host_scale_test"
