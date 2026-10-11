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
"$CC_BIN" -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra -Werror -g -O1 \
  -fsanitize=address,undefined -fno-omit-frame-pointer -pthread \
  -I"$ROOT/tools/touch_async_stub" -I"$ROOT/firmware/touch" \
  "$ROOT/firmware/touch/touch_async.c" "$ROOT/tools/touch_async_test.c" \
  -o "$OUT/touch_async_test"
ASAN_OPTIONS=detect_leaks=1 "$OUT/touch_async_test"
