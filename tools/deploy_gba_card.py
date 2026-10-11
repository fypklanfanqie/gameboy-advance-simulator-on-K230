#!/usr/bin/env python3
"""Deploy four runtime Python files to an explicitly selected card directory.

The default is a read-only plan. --write backs up existing targets outside the
card, flushes/fsyncs each write, and verifies SHA256 readback. --rom explicitly
adds a user-owned .gba file; existing different ROM data is never overwritten.
Save files and raw firmware slots are never written.
"""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
FILES = [(SRC / "main.py", "main.py"),
         (SRC / "gba_ui.py", "gba_ui.py"),
         (SRC / "gba_input.py", "gba_input.py"),
         (SRC / "gba_audio.py", "gba_audio.py")]

def card_root(value):
    if os.name == "nt" and len(value) == 2 and value[1] == ":":
        value += "\\"
    return Path(value).resolve()

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def save_hashes(root):
    return {str(path.relative_to(root)): sha256_file(path)
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in (".sav", ".state")}

def write_verified(data, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".deploy-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256_file(temp) != hashlib.sha256(data).hexdigest():
            raise RuntimeError("temporary-file readback mismatch: " + str(destination))
        os.replace(temp, destination)
        if os.name != "nt":
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if sha256_file(destination) != hashlib.sha256(data).hexdigest():
            raise RuntimeError("destination readback mismatch: " + str(destination))
    finally:
        if temp.exists():
            temp.unlink()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive", required=True, help="card mount directory, e.g. F: or /media/user/SDCARD")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--rom", type=Path, help="explicit external .gba to add under roms/")
    parser.add_argument("--audio-probe-once", action="store_true",
                        help="also deploy the optional tone probe and one-time boot marker")
    parser.add_argument("--backup-dir", type=Path, default=PROJECT_ROOT / "build/deploy-backups")
    args = parser.parse_args()
    root = card_root(args.drive)
    if not root.is_dir():
        parser.error("card mount directory does not exist: " + str(root))
    if root == PROJECT_ROOT or root in PROJECT_ROOT.parents or PROJECT_ROOT in root.parents:
        parser.error("card path must be outside the repository")
    plan = [(source, root / name) for source, name in FILES]
    if args.audio_probe_once:
        plan.append((PROJECT_ROOT / "tools/audio_tone_probe.py", root / "audio_tone_probe.py"))
    if args.rom is not None:
        args.rom = args.rom.resolve()
        if not args.rom.is_file() or args.rom.suffix.lower() != ".gba":
            parser.error("--rom must be an existing external .gba file")
        target = root / "roms" / args.rom.name
        if target.exists() and sha256_file(target) != sha256_file(args.rom):
            parser.error("existing different ROM is protected: " + str(target))
        plan.append((args.rom, target))
    for source, destination in plan:
        if not source.is_file():
            parser.error("missing source: " + str(source))
        if destination.is_symlink():
            parser.error("refusing symlink target: " + str(destination))
        if root not in destination.resolve().parents:
            parser.error("target escapes selected card directory")
        print("%s -> %s (%d bytes)" % (source.name, destination, source.stat().st_size))
    if not args.write:
        print("DRY RUN: no files written; use --write to deploy")
        return 0
    backup_root = args.backup_dir.resolve()
    if backup_root == root or root in backup_root.parents:
        parser.error("backup directory must be outside the card")
    backup = backup_root / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    before_saves = save_hashes(root)
    backup.mkdir(parents=True)
    marker = root / "audio_probe.once"
    targets = [destination for _, destination in plan]
    if args.audio_probe_once:
        targets.append(marker)
    for destination in targets:
        if destination.exists():
            prior = backup / destination.relative_to(root)
            prior.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, prior)
            if sha256_file(prior) != sha256_file(destination):
                raise RuntimeError("backup mismatch: " + str(destination))
    for source, destination in plan:
        write_verified(source.read_bytes(), destination)
        print("PASS readback: " + str(destination.relative_to(root)))
    if args.audio_probe_once:
        write_verified(b"left right same opposite then game, once\n", marker)
    if save_hashes(root) != before_saves:
        raise RuntimeError("save identity changed during deployment")
    print("PASS: deployment readback and save identity; backups: " + str(backup))
    return 0

if __name__ == "__main__":
    sys.exit(main())
