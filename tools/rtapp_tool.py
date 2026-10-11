#!/usr/bin/env python3
"""Generate and verify a genuine vendor RTAPP package for the K230 A/B layout.

WHY THIS EXISTS
---------------
The vendor script tools/gen_image_rtapp.py prints "Generate RTApp Done." on BOTH
the genuine and the fake path, so command success proves nothing. It silently
falls back to a 4096-zero-byte "fake" RTAPP whenever CONFIG_FAST_BOOT_FILE_PATH
cannot be read. Additionally, in this tree `prepare` (a prerequisite of both
`canmv` and `app`) runs `rm -rf $(SDK_BUILD_IMAGES_DIR)`, so a plain top-level
`make` wipes images/sdcard/micropython before the packer runs - producing the
fake while still reporting success.

This tool therefore:
  1. copies the built ELF into the exact path the .config expects,
  2. invokes the official packer and captures its log,
  3. proves genuineness using the packer's OWN positive signal
     (images/bin/preload exists => genuine; it is DELETED on the fake path),
  4. unpacks the result and asserts the decompressed ELF is byte-identical to
     the build output.

FORMAT NOTES (verified against the real vendor packages, see
docs/ and the RTAPP investigation report):
  outer 528-byte K230 header:
    0   4  magic "K230"
    4   4  uint32 LE length = filesize - 528
    8   4  uint32 LE crypto_type (0 = none)
    12  32 SHA-256 of bytes[528:]
    44  484 zeros
  then at 528:
    528  4  version bytes (zeros)
    532 64  uImage header: magic 0x27051956, os=17 (rtapp), arch=26 (riscv),
            type=5 (firmware), comp=1 (gzip)
    596 ..  private gzip stream, but with byte[2] == 0x09 instead of 0x08
  The vendor's own k230_priv_gzip CANNOT read its own output ("unknown method
  9 -- not supported"), so the byte must be flipped back to 0x08 before
  decompressing. That flip is only applied on the compress path.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import struct
import sys
import zlib

OUTER_HEADER = 528
UIMAGE_HEADER = 64
PAYLOAD_OFFSET = OUTER_HEADER + 4 + UIMAGE_HEADER  # 596
FAKE_MIN = 4096  # a genuine ELF is ~20 MB; the fake gzips to a few hundred bytes


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_outer_header(blob):
    if len(blob) < OUTER_HEADER:
        raise ValueError("truncated K230 outer header")
    if blob[:4] != b"K230":
        raise ValueError("missing K230 magic (not a vendor package)")
    length = int.from_bytes(blob[4:8], "little")
    crypto = int.from_bytes(blob[8:12], "little")
    digest = blob[12:44]
    reserved = blob[44:528]
    return {
        "length_field": length,
        "computed_length": len(blob) - OUTER_HEADER,
        "crypto_type": crypto,
        "sha256_field": digest.hex(),
        "sha256_actual": hashlib.sha256(blob[OUTER_HEADER:]).hexdigest(),
        "reserved_all_zero": reserved == b"\x00" * len(reserved),
    }


def parse_uimage_header(blob):
    """Validate uImage CRCs against the original private-gzip bytes.

    The real vendor package has a big-endian 64-byte uImage header. Its data
    CRC includes method 0x09; normalization to 0x08 happens only afterwards.
    Timestamp, load/entry addresses, and the descriptive name are not fixed.
    """
    if len(blob) <= PAYLOAD_OFFSET:
        raise ValueError("package too small to contain a payload")
    if blob[OUTER_HEADER:OUTER_HEADER + 4] != b"\x00" * 4:
        raise ValueError("unsupported RTAPP version prefix")
    header = blob[OUTER_HEADER + 4:PAYLOAD_OFFSET]
    fields = struct.unpack(">7I4B32s", header)
    magic, hcrc, timestamp, size, load, entry, dcrc, os_id, arch, kind, comp, name = fields
    if magic != 0x27051956:
        raise ValueError("invalid uImage magic")
    cleared = header[:4] + b"\x00" * 4 + header[8:]
    if zlib.crc32(cleared) & 0xFFFFFFFF != hcrc:
        raise ValueError("uImage header CRC mismatch")
    if (os_id, arch, kind, comp) != (17, 26, 5, 1):
        raise ValueError("unsupported uImage os/arch/type/comp: %r" %
                         ((os_id, arch, kind, comp),))
    payload = blob[PAYLOAD_OFFSET:]
    if size != len(payload):
        raise ValueError("uImage data length mismatch: %d/%d" % (size, len(payload)))
    if zlib.crc32(payload) & 0xFFFFFFFF != dcrc:
        raise ValueError("uImage data CRC mismatch")
    return {"data_size": size, "header_crc": hcrc, "data_crc": dcrc,
            "os": os_id, "arch": arch, "type": kind, "comp": comp}


def extract_elf(blob):
    """Return the decompressed ELF bytes from a vendor RTAPP package."""
    if len(blob) <= PAYLOAD_OFFSET:
        raise ValueError("package too small to contain a payload")
    payload = blob[PAYLOAD_OFFSET:]
    if len(payload) < 18 or payload[:2] != b"\x1f\x8b":
        raise ValueError("payload is not gzip (expected 1f 8b)")
    method = payload[2]
    if method not in (0x08, 0x09):
        raise ValueError("unsupported gzip method 0x%02x" % method)
    normal = payload if method == 0x08 else payload[:2] + b"\x08" + payload[3:]
    # Verify CRC/ISIZE explicitly so a corrupt stream is reported clearly.
    d = zlib.decompressobj(31)
    try:
        out = d.decompress(normal)
        out += d.flush()
    except zlib.error as exc:
        raise ValueError("gzip stream invalid (CRC/ISIZE or compressed data): %s" % exc) from exc
    if not d.eof:
        raise ValueError("gzip stream incomplete (truncated package)")
    if d.unused_data:
        raise ValueError("trailing data after gzip stream (%d bytes)" % len(d.unused_data))
    if out == b"\x00" * FAKE_MIN:
        raise ValueError("vendor fake RTAPP (4096 zero bytes)")
    if out[:4] != b"\x7fELF":
        raise ValueError("decompressed payload is not an ELF")
    return out, method


def validate_package(blob):
    """Shared structural validation for both generation and verification."""
    outer = parse_outer_header(blob)
    if outer["length_field"] != outer["computed_length"]:
        raise ValueError("K230 outer length mismatch")
    if outer["crypto_type"] != 0:
        raise ValueError("unsupported K230 crypto_type")
    if outer["sha256_field"] != outer["sha256_actual"]:
        raise ValueError("K230 outer SHA-256 mismatch")
    if not outer["reserved_all_zero"]:
        raise ValueError("K230 outer reserved bytes are nonzero")
    uimage = parse_uimage_header(blob)
    elf, method = extract_elf(blob)
    return outer, uimage, elf, method


def cmd_generate(args):
    sdk = args.sdk
    build_images = os.path.join(sdk, "output", args.defconfig, "images")
    built_elf = os.path.join(sdk, "output", args.defconfig, "canmv", "micropython")
    expected_dest = os.path.join(build_images, "sdcard", "micropython")

    if not os.path.isfile(built_elf):
        print("ERROR: built ELF not found: %s" % built_elf)
        print("       build CanMV first: make -C src/canmv build NCPUS=4")
        return 1

    elf_sha = sha256_file(built_elf)
    elf_size = os.path.getsize(built_elf)
    print("built ELF   : %s" % built_elf)
    print("  size      : %d" % elf_size)
    print("  sha256    : %s" % elf_sha)

    # The packer deletes images/bin/preload on the fake path; clear it first so
    # its later presence is meaningful evidence for THIS run.
    preload = os.path.join(build_images, "bin", "preload")
    if os.path.exists(preload):
        os.remove(preload)
        print("removed stale %s (so its reappearance proves this run was genuine)" % preload)
    rtapp_dir = os.path.join(build_images, "rtapp")
    rtapp_out = os.path.join(rtapp_dir, "rtapp.elf.gz")
    if os.path.exists(rtapp_out):
        os.remove(rtapp_out)
        print("removed stale %s" % rtapp_out)

    os.makedirs(os.path.dirname(expected_dest), exist_ok=True)
    shutil.copyfile(built_elf, expected_dest)
    print("staged ELF  : %s" % expected_dest)
    print("  sha256    : %s" % sha256_file(expected_dest))

    env = dict(os.environ)
    env["SDK_SRC_ROOT_DIR"] = sdk
    env["SDK_BUILD_IMAGES_DIR"] = build_images
    # NOTE: CONFIG_FAST_BOOT_FILE_PATH cannot be overridden on the make command
    # line; the script reads .config from disk. So we satisfy the existing value.
    script = os.path.join(sdk, "tools", "gen_image_rtapp.py")
    print("\n--- running %s ---" % script)
    proc = subprocess.run([sys.executable, script], cwd=sdk, env=env,
                          capture_output=True, text=True)
    print(proc.stdout)
    if proc.stderr.strip():
        print("stderr:\n%s" % proc.stderr)
    print("exit code: %d" % proc.returncode)
    if proc.returncode != 0:
        print("FAIL: official RTAPP packer failed")
        return 1

    log = proc.stdout + proc.stderr
    fake_markers = ["Use fake rtapp", "Generated fake RT-App", "An error occurred:"]
    hit = [m for m in fake_markers if m in log]
    if "Generate RTApp Done." in log:
        print("\nNOTE: 'Generate RTApp Done.' is printed on BOTH paths - not evidence.")

    if not os.path.isfile(rtapp_out):
        print("\nFAIL: no RTAPP produced at %s" % rtapp_out)
        return 1

    print("\n=== genuineness checks ===")
    ok = True
    if hit:
        print("FAIL: fake-path marker(s) in log: %s" % hit)
        ok = False
    else:
        print("ok  : no fake-path marker in log")
    if os.path.exists(preload):
        print("ok  : images/bin/preload EXISTS (packer's positive signal)")
    else:
        print("FAIL: images/bin/preload absent -> fake path was taken")
        ok = False
    size = os.path.getsize(rtapp_out)
    if size > FAKE_MIN * 10:
        print("ok  : packed size %d bytes (fake would be < ~1 KiB)" % size)
    else:
        print("FAIL: packed size %d bytes is fake-sized" % size)
        ok = False
    if not ok:
        return 1

    print("\n=== payload verification ===")
    with open(rtapp_out, "rb") as fh:
        blob = fh.read()
    try:
        hdr, uimage, elf, method = validate_package(blob)
    except ValueError as exc:
        print("FAIL: %s" % exc)
        return 1
    print("outer length field : %d (computed %d) %s"
          % (hdr["length_field"], hdr["computed_length"],
             "OK" if hdr["length_field"] == hdr["computed_length"] else "MISMATCH"))
    print("crypto_type        : %d %s" % (hdr["crypto_type"],
          "OK" if hdr["crypto_type"] == 0 else "UNEXPECTED"))
    print("outer sha256       : %s %s"
          % ("OK" if hdr["sha256_field"] == hdr["sha256_actual"] else "MISMATCH",
             hdr["sha256_field"]))
    print("reserved zeroed    : %s" % ("OK" if hdr["reserved_all_zero"] else "NOT ZERO"))
    print("uImage header/data : CRCs OK, size=%d, os/arch/type/comp=17/26/5/1" %
          uimage["data_size"])
    print("gzip method byte   : 0x%02x %s" % (method,
          "(vendor private gzip)" if method == 0x09 else "(standard)"))
    print("unpacked size      : %d %s" % (len(elf),
          "OK" if len(elf) == elf_size else "MISMATCH (expected %d)" % elf_size))
    got = hashlib.sha256(elf).hexdigest()
    print("unpacked sha256    : %s %s" % (got, "OK" if got == elf_sha else "MISMATCH"))
    if len(elf) != elf_size or got != elf_sha:
        print("\nFAIL: unpacked ELF does not match the build output")
        return 1

    print("\nSUCCESS: %s" % rtapp_out)
    print("  packed size : %d" % size)
    print("  ELF sha256  : %s" % elf_sha)
    # Persist the evidence next to the artifact.
    with open(rtapp_out + ".verify.txt", "w") as fh:
        fh.write("source_elf=%s\n" % built_elf)
        fh.write("source_elf_size=%d\n" % elf_size)
        fh.write("source_elf_sha256=%s\n" % elf_sha)
        fh.write("rtapp=%s\n" % rtapp_out)
        fh.write("rtapp_size=%d\n" % size)
        fh.write("rtapp_sha256=%s\n" % sha256_file(rtapp_out))
        fh.write("gzip_method=0x%02x\n" % method)
        fh.write("verified=byte-identical\n")
    print("  evidence    : %s.verify.txt" % rtapp_out)
    return 0


def cmd_verify(args):
    """Verify an existing package against an expected ELF (no build needed)."""
    try:
        with open(args.rtapp, "rb") as fh:
            blob = fh.read()
        hdr, uimage, elf, method = validate_package(blob)
    except (OSError, ValueError) as exc:
        print("FAIL: %s" % exc)
        return 1
    print("package     : %s" % args.rtapp)
    print("size        : %d" % len(blob))
    print("sha256      : %s" % hashlib.sha256(blob).hexdigest())
    print("outer len   : %d/%d %s" % (hdr["length_field"], hdr["computed_length"],
          "OK" if hdr["length_field"] == hdr["computed_length"] else "MISMATCH"))
    print("outer sha256: %s" % ("OK" if hdr["sha256_field"] == hdr["sha256_actual"] else "MISMATCH"))
    print("crypto_type : %d" % hdr["crypto_type"])
    print("reserved    : %s" % ("zeroed" if hdr["reserved_all_zero"] else "NON-ZERO"))
    print("uImage CRCs : header/data OK; os/arch/type/comp=17/26/5/1")
    print("gzip method : 0x%02x" % method)
    print("unpacked    : %d bytes" % len(elf))
    print("unpacked sha: %s" % hashlib.sha256(elf).hexdigest())
    if args.expected:
        try:
            exp_sha = sha256_file(args.expected)
            exp_size = os.path.getsize(args.expected)
        except OSError as exc:
            print("FAIL: expected ELF cannot be read: %s" % exc)
            return 1
        match = (len(elf) == exp_size and hashlib.sha256(elf).hexdigest() == exp_sha)
        print("expected    : %s (%d bytes, %s)" % (args.expected, exp_size, exp_sha))
        print("VERDICT     : %s" % ("BYTE-IDENTICAL" if match else "MISMATCH"))
        return 0 if match else 1
    print("VERDICT     : VALID RTAPP (no expected ELF supplied)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="stage the built ELF, run the official packer, and verify")
    g.add_argument("--sdk", default=os.environ.get("SDK_PATH"),
                   help="SDK directory (or set SDK_PATH)")
    g.add_argument("--defconfig", default="k230_canmv_hiwonder_defconfig")
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("verify", help="verify an existing package (read-only)")
    v.add_argument("rtapp")
    v.add_argument("--expected", help="ELF to compare against")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    if args.cmd == "generate" and args.sdk is None:
        ap.error("generate requires --sdk or SDK_PATH")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
