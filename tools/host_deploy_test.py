#!/usr/bin/env python3
"""Test card deployment protections using isolated temporary directories."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    checks = 0
    with tempfile.TemporaryDirectory(prefix="k230-deploy-test-") as directory:
        base = Path(directory)
        card = base / "card"
        card.mkdir()
        save = card / "roms/user.sav"
        save.parent.mkdir()
        save.write_bytes(b"protected save data")
        (card / "main.py").write_bytes(b"previous main")
        backup = base / "backups"
        command = [sys.executable, str(ROOT / "tools/deploy_gba_card.py"),
                   "--drive", str(card), "--backup-dir", str(backup)]
        def run(extra=()):
            return subprocess.run(command + list(extra), capture_output=True)
        assert run().returncode == 0
        assert (card / "main.py").read_bytes() == b"previous main"
        assert not backup.exists()
        checks += 3
        assert run(["--write"]).returncode == 0
        for name in ("main.py", "gba_ui.py", "gba_input.py", "gba_audio.py"):
            assert (card / name).read_bytes() == (ROOT / "src" / name).read_bytes()
            checks += 1
        assert save.read_bytes() == b"protected save data"
        assert not (card / "audio_tone_probe.py").exists()
        assert not (card / "audio_probe.once").exists()
        assert next(backup.rglob("main.py")).read_bytes() == b"previous main"
        checks += 4
        rom = base / "homebrew.gba"
        rom.write_bytes(b"external homebrew")
        assert run(["--write", "--rom", str(rom)]).returncode == 0
        assert (card / "roms/homebrew.gba").read_bytes() == rom.read_bytes()
        checks += 2
        rom.write_bytes(b"different ROM")
        assert run(["--write", "--rom", str(rom)]).returncode != 0
        assert (card / "roms/homebrew.gba").read_bytes() == b"external homebrew"
        checks += 2
        assert run(["--write", "--audio-probe-once"]).returncode == 0
        assert (card / "audio_tone_probe.py").read_bytes() == (ROOT / "tools/audio_tone_probe.py").read_bytes()
        assert (card / "audio_probe.once").read_bytes() == b"left right same opposite then game, once\n"
        assert save.read_bytes() == b"protected save data"
        checks += 4
    print("Deployment protection regression: %d checks, PASS" % checks)
    return 0

if __name__ == "__main__":
    sys.exit(main())
