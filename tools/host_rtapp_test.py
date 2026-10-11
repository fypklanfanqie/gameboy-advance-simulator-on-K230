#!/usr/bin/env python3
"""ROM-free regression tests for K230 RTAPP verification and generation.

All artifacts are synthetic and live in a temporary directory. Neither the
vendor SDK nor an SD card is needed or modified by these tests.
Run: python tools/host_rtapp_test.py
"""

import contextlib
import gzip
import hashlib
import io
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zlib

import rtapp_tool as rtapp


def outer(body):
    return (b"K230" + struct.pack("<II", len(body), 0) +
            hashlib.sha256(body).digest() + bytes(484) + body)


def package(elf=None, method=9, payload=None):
    if payload is None:
        payload = gzip.compress(elf, mtime=0)
        payload = payload[:2] + bytes([method]) + payload[3:]
    fields = (0x27051956, 0, 123456789, len(payload), 0, 0,
              zlib.crc32(payload) & 0xFFFFFFFF, 17, 26, 5, 1, b"rtapp")
    header = struct.pack(">7I4B32s", *fields)
    hcrc = zlib.crc32(header) & 0xFFFFFFFF
    header = header[:4] + struct.pack(">I", hcrc) + header[8:]
    return outer(bytes(4) + header + payload)


def set_bytes(blob, start, value):
    return blob[:start] + value + blob[start + len(value):]


def repair_outer(blob):
    return outer(blob[rtapp.OUTER_HEADER:])


def change_header(blob, offset, value, repair_crc=True):
    header_at = rtapp.OUTER_HEADER + 4
    blob = set_bytes(blob, header_at + offset, value)
    if repair_crc:
        header = blob[header_at:rtapp.PAYLOAD_OFFSET]
        cleared = header[:4] + bytes(4) + header[8:]
        blob = set_bytes(blob, header_at + 4,
                         struct.pack(">I", zlib.crc32(cleared) & 0xFFFFFFFF))
    return repair_outer(blob)


def quiet_call(func, args):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        result = func(args)
    return result, stream.getvalue()


def main():
    checks = 0

    def check(condition, description):
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(description)

    # This is an ELF-shaped fixture, not an executable or a ROM. The tool's
    # contract validates ELF magic and exact identity with an expected build.
    elf = b"\x7fELF" + bytes(range(256)) * 20
    base = package(elf)
    stream = base[rtapp.PAYLOAD_OFFSET:]
    cases = [
        ("truncated outer header", base[:527], "truncated K230"),
        ("outer magic", set_bytes(base, 0, b"BAD!"), "missing K230"),
        ("outer length", set_bytes(base, 4, struct.pack("<I", 0)), "outer length"),
        ("outer SHA", set_bytes(base, 12, bytes(32)), "outer SHA"),
        ("crypto type", set_bytes(base, 8, struct.pack("<I", 1)), "crypto_type"),
        ("reserved bytes", set_bytes(base, 527, b"\x01"), "reserved"),
        ("missing uImage", outer(bytes(4)), "too small"),
        ("truncated uImage", outer(bytes(4) + bytes(63)), "too small"),
        ("missing gzip", outer(base[528:596]), "too small"),
        ("version prefix", repair_outer(set_bytes(base, 528, b"\x01")), "version"),
        ("uImage magic", change_header(base, 0, bytes(4)), "uImage magic"),
        ("header CRC", change_header(base, 4, bytes(4), False), "header CRC"),
        ("data length", change_header(base, 12, struct.pack(">I", len(stream) + 1)), "data length"),
        ("data CRC", change_header(base, 24, bytes(4)), "data CRC"),
        ("OS", change_header(base, 28, b"\x00"), "os/arch/type/comp"),
        ("architecture", change_header(base, 29, b"\x00"), "os/arch/type/comp"),
        ("type", change_header(base, 30, b"\x00"), "os/arch/type/comp"),
        ("compression", change_header(base, 31, b"\x00"), "os/arch/type/comp"),
        ("short gzip", package(payload=stream[:17]), "not gzip"),
        ("gzip magic", package(payload=set_bytes(stream, 0, b"NO")), "not gzip"),
        ("unknown gzip method", package(elf, method=7), "unsupported gzip method"),
        ("gzip flags", package(payload=set_bytes(stream, 3, b"\xe0")), "gzip stream invalid"),
        ("gzip compressed data", package(payload=set_bytes(stream, 10, b"\xff")), "gzip stream invalid"),
        ("gzip CRC", package(payload=set_bytes(stream, len(stream) - 8, bytes(4))), "gzip stream invalid"),
        ("gzip ISIZE", package(payload=set_bytes(stream, len(stream) - 4, bytes(4))), "gzip stream invalid"),
        ("gzip truncated footer", package(payload=stream[:-1]), "incomplete"),
        ("gzip truncated deflate", package(payload=stream[:30]), "incomplete"),
        ("trailing gzip data", package(payload=stream + b"extra"), "trailing data"),
        ("concatenated gzip member", package(payload=stream + gzip.compress(b"second")), "trailing data"),
        ("non-ELF", package(b"not an ELF"), "not an ELF"),
        ("empty payload", package(b""), "not an ELF"),
        ("vendor fake", package(bytes(4096)), "fake RTAPP"),
    ]

    with tempfile.TemporaryDirectory(prefix="pokemo-rtapp-test-") as tmp:
        root = Path(tmp)
        candidate = root / "candidate.elf.gz"
        expected = root / "expected.elf"
        expected.write_bytes(elf)
        for method in (8, 9):
            blob = package(elf, method=method)
            header, uimage, actual, actual_method = rtapp.validate_package(blob)
            check(actual == elf and actual_method == method, "valid gzip method %d" % method)
            check(uimage["data_crc"] == zlib.crc32(blob[596:]) & 0xFFFFFFFF,
                  "data CRC covers original method %d" % method)
            candidate.write_bytes(blob)
            result, output = quiet_call(rtapp.cmd_verify,
                                       SimpleNamespace(rtapp=str(candidate), expected=None))
            check(result == 0 and "VALID RTAPP" in output, "valid structure without expected ELF")
            result, output = quiet_call(rtapp.cmd_verify,
                                       SimpleNamespace(rtapp=str(candidate), expected=str(expected)))
            check(result == 0 and "BYTE-IDENTICAL" in output, "matching expected ELF")

        for name, blob, message in cases:
            try:
                rtapp.validate_package(blob)
            except ValueError as exc:
                check(message in str(exc), "%s failed at intended layer: %s" % (name, exc))
            else:
                check(False, "accepted corrupt package: " + name)
            candidate.write_bytes(blob)
            for expected_path in (None, str(expected)):
                result, output = quiet_call(rtapp.cmd_verify,
                                           SimpleNamespace(rtapp=str(candidate), expected=expected_path))
                check(result != 0 and message in output, "CLI rejected %s (expected=%r)" % (name, expected_path))

        candidate.write_bytes(base)
        wrong = root / "wrong.elf"
        wrong.write_bytes(elf[:-1] + bytes([elf[-1] ^ 1]))
        for expected_path in (str(wrong), str(root / "missing.elf")):
            result, _ = quiet_call(rtapp.cmd_verify,
                                   SimpleNamespace(rtapp=str(candidate), expected=expected_path))
            check(result != 0, "mismatched or unreadable expected ELF rejected")
        result, _ = quiet_call(rtapp.cmd_verify,
                               SimpleNamespace(rtapp=str(root / "missing.gz"), expected=None))
        check(result != 0, "unreadable package rejected")
        expected.write_bytes(bytes(4096))
        candidate.write_bytes(package(bytes(4096)))
        result, output = quiet_call(rtapp.cmd_verify,
                                   SimpleNamespace(rtapp=str(candidate), expected=str(expected)))
        check(result != 0 and "fake RTAPP" in output, "matching expected fake remains rejected")

        # Exercise generate without executing the official packer or touching
        # a real SDK. Large hash-derived data passes its fake-size heuristic.
        built_elf = b"\x7fELF" + b"".join(hashlib.sha256(struct.pack("<I", n)).digest()
                                         for n in range(2500))
        sdk = root / "sdk"
        images = sdk / "output" / "test_defconfig" / "images"
        built = sdk / "output" / "test_defconfig" / "canmv" / "micropython"
        built.parent.mkdir(parents=True)
        built.write_bytes(built_elf)
        args = SimpleNamespace(sdk=str(sdk), defconfig="test_defconfig")
        generated = images / "rtapp" / "rtapp.elf.gz"
        evidence = Path(str(generated) + ".verify.txt")
        preload = images / "bin" / "preload"

        def generate_test(name, blob, status=0, log="Generate RTApp Done.", have_preload=True):
            if evidence.exists():
                evidence.unlink()

            def fake_packer(*unused, **kwargs):
                check(kwargs["env"]["SDK_BUILD_IMAGES_DIR"] == str(images), "mock SDK path")
                check((images / "sdcard" / "micropython").read_bytes() == built_elf, "staged ELF identity")
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_bytes(blob)
                if have_preload:
                    preload.parent.mkdir(parents=True, exist_ok=True)
                    preload.write_bytes(b"mock positive marker")
                return SimpleNamespace(returncode=status, stdout=log, stderr="")

            with patch.object(rtapp.subprocess, "run", side_effect=fake_packer):
                result, output = quiet_call(rtapp.cmd_generate, args)
            return result, output

        valid_large = package(built_elf)
        result, output = generate_test("valid", valid_large)
        check(result == 0 and evidence.exists(), "generate validates and writes evidence")
        generator_cases = [
            ("outer length", set_bytes(valid_large, 4, bytes(4)), {}, "outer length"),
            ("header CRC", change_header(valid_large, 4, bytes(4), False), {}, "header CRC"),
            ("data CRC", change_header(valid_large, 24, bytes(4)), {}, "data CRC"),
            ("unknown method", package(built_elf, method=7), {}, "unsupported gzip method"),
            ("payload mismatch", package(built_elf[:-1] + b"x"), {}, "does not match"),
            ("packer failure", valid_large, {"status": 1}, "packer failed"),
            ("fake-path log", valid_large, {"log": "Use fake rtapp"}, "fake-path marker"),
            ("missing positive marker", valid_large, {"have_preload": False}, "preload absent"),
            ("fake-sized output", base, {}, "fake-sized"),
        ]
        for name, blob, options, message in generator_cases:
            result, output = generate_test(name, blob, **options)
            check(result != 0 and message in output and not evidence.exists(),
                  "generate rejected %s without success evidence" % name)

    print("RTAPP host regression: %d checks, 0 failures, PASS" % checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
