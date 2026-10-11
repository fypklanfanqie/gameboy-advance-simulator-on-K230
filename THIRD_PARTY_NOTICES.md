# License scope and third-party notices

The root MIT license applies to original K230 integration code, Python
runtime code, project tools, and project documentation. It does not replace
the licenses of bundled third-party code or claim ownership of that code.
Commercial use is permitted by the original-project MIT license; no
noncommercial restriction is added. Each third-party component retains its
own conditions. Existing source notices must be preserved.

Original integration files include `firmware/gba/gba_module.c`,
`firmware/gba/gba_scale.c`, `firmware/gba/gba_scale.h`,
`firmware/gba/hello.c`, and `firmware/touch/touch_async.c` and `.h`.
The modified vendor touch binding retains the vendor BSD-2-Clause notice.
Files derived from mGBA retain their upstream licensing even when they have
been adapted for K230.

## Bundled mGBA code

The mGBA 0.10.5-derived files in `firmware/gba/core/` and
`firmware/gba/include/` are primarily licensed under MPL-2.0.
Copyright notices identify Jeffrey Pfau and the relevant contributors.
The exceptions below retain their own licenses. The generated K230
`core/core/version.c`, generated GBK mapping table, and generated HLE BIOS
representation are part of this upstream-derived tree, not an assertion of
new MIT ownership.

Upstream: <https://github.com/mgba-emu/mgba>

License text: [MPL-2.0](LICENSES/MPL-2.0.txt).

The files `core/gba/hle-bios.s`, `hle-bios.make`, `hle-bios.h`, and
`hle-bios.c` contain the open-source mGBA replacement BIOS and its generation
recipe. The assembly is the preferred form for modification; the C byte
array is generated from it. This replacement is distinct from a proprietary
Nintendo BIOS dump. No proprietary game ROM, user save, or original-console
BIOS image is supplied with this repository.

MPL-covered source and modifications remain available under MPL-2.0. When
distributing an executable containing this code, provide access to its
corresponding MPL-covered source and inform recipients where to obtain it.

## blip_buf 1.1.0

Files: `firmware/gba/core/util/blip_buf.c` and
`firmware/gba/include/include/mgba/core/blip_buf.h`.

Copyright (C) 2003-2009 Shay Green.

The actual bundled `blip_buf.c` declares **LGPL-2.1-or-later**. It is not
licensed under ISC in this source snapshot. The header is distributed as
part of that library.

License text: [LGPL-2.1](LICENSES/LGPL-2.1.txt).
The accompanying [GPL-2 text](LICENSES/GPL-2.txt) is retained for the
LGPL-2.1 terms; its presence does not relabel the remaining repository GPL.

Upstream: <http://www.slack.net/~ant/>

For a binary release that links this library, satisfy the applicable LGPL
requirements, including source availability for the library and its
modifications, preservation of notices, and the necessary means to modify
or relink the library. Publishing this source snapshot alone is not a claim
that every possible firmware binary distribution meets those requirements.

## inih

Files: `firmware/gba/core/third-party/inih/`, including parser, header,
examples, fuzzing helpers, and tests.

Copyright (C) 2009-2025 Ben Hoyt, as stated in the parser source.
License: BSD-3-Clause. The upstream license text retains its original
copyright year; the source headers retain the later year range.

License text: [inih BSD-3-Clause](LICENSES/inih-BSD-3-Clause.txt).
Upstream: <https://github.com/benhoyt/inih>

## Canaan CanMV touch binding

Files: `firmware/touch/machine_touch.vendor.c`,
`firmware/touch/machine_touch.c`, and the corresponding vendor-code portions
of any supplied patch.

Copyright (c) 2025 Canaan Bright Sight Co., Ltd.
License: BSD-2-Clause. The complete vendor notice is retained in the source
and in [Canaan BSD-2-Clause](LICENSES/Canaan-BSD-2-Clause.txt).

The original native worker in `touch_async.c` and `touch_async.h` is covered
by the project MIT license. This does not change the license of the vendor
binding with which it is used.

## Additional source-level notices

- `firmware/gba/core/util/hash.c`: MurmurHash3 by Austin Appleby. The source
  explicitly places it in the public domain and disclaims copyright.
- `firmware/gba/core/util/crc32.c`: Copyright (C) 1986 Gary S. Brown. Its
  source notice permits use of the program, code, and extracted tables as
  desired without restriction. Preserve the notice and attribution.
- `firmware/gba/include/include/mgba-util/platform/windows/getopt.h`:
  mingw-w64 runtime source, explicitly placed in the public domain, with its
  warranty disclaimer retained.

## External build dependencies

The complete CanMV SDK, RT-Smart kernel, MicroPython, media stack, compiler,
and build dependencies are not relicensed by this repository. Obtain them
from the documented upstream or vendor sources and comply with their own
licenses. A full firmware image may contain additional components beyond
the files supplied here; audit those components before distributing such
an image.
