"""Run repository-owned native test scripts on Linux or Windows WSL."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SKIP = 77

def wsl_prefix():
    prefix = ["wsl"]
    distro = os.environ.get("POKEMO_WSL_DISTRO")
    if distro:
        prefix.extend(["-d", distro])
    return prefix + ["--"]

def decode_output(data):
    return data.decode("utf-16-le" if b"\0" in data else "utf-8", errors="replace")

def native_path(path):
    path = str(Path(path).resolve())
    if os.name != "nt":
        return path
    result = subprocess.run(wsl_prefix() + ["wslpath", "-a", path], capture_output=True)
    if result.returncode:
        raise RuntimeError("WSL path conversion failed: " + decode_output(result.stderr).strip())
    return decode_output(result.stdout).strip()

def run_native(script, args=()):
    if os.name == "nt" and not shutil.which("wsl"):
        return SKIP, "SKIP: WSL is unavailable; native tests require Linux and a C compiler"
    if os.name != "nt" and not shutil.which("bash"):
        return SKIP, "SKIP: bash is unavailable"
    try:
        path = native_path(ROOT / "tools/wsl" / script)
        command = (["bash", path] if os.name != "nt" else wsl_prefix() + ["bash", path])
        result = subprocess.run(command + list(args), cwd=ROOT, capture_output=True)
    except (OSError, RuntimeError) as exc:
        return SKIP, "SKIP: native environment unavailable: " + str(exc)
    return result.returncode, decode_output(result.stdout) + decode_output(result.stderr)
