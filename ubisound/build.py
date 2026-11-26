#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools" / "buildtools"))
import posixrun

COMPILER_URL = "https://github.com/decompme/compilers/releases/download/compilers/iop-gcc2.8.1.tar.gz"
COMPILER_SHA256 = "1f602e146f5be218cb3c84cda615432035bcf4a4f1a5f249164163a456ac929a"
COMPILER = HERE / "tools" / "iop-gcc2.8.1"


def install_compiler() -> None:
    if (COMPILER / "bin" / "iop-gcc").is_file():
        return
    with urllib.request.urlopen(COMPILER_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != COMPILER_SHA256:
        raise SystemExit("iop-gcc2.8.1.tar.gz: hash mismatch")
    COMPILER.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(COMPILER, filter="data")


def split() -> None:
    for name in ("asm", "assets", "include", "build"):
        shutil.rmtree(HERE / name, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "splat", "split", "ubisound.yaml"],
                   cwd=HERE, check=True, stdout=subprocess.DEVNULL)
    labels = HERE / "include" / "labels.inc"
    labels.write_text(re.sub(r"^(\s*)\.aent \\label$", r"\1# .aent \\label",
                             labels.read_text(), flags=re.M))


def main() -> int:
    target = ROOT / "iso" / "UBISOUND.IRX"
    expected = re.search(r"^sha1: (\w+)", (HERE / "ubisound.yaml").read_text(), re.M).group(1)
    if not target.is_file() or hashlib.sha1(target.read_bytes()).hexdigest() != expected:
        raise SystemExit(f"{target}: missing or not the expected file (sha1 {expected})")
    install_compiler()
    split()
    if posixrun.run("sh ubisound/tools/build_irx.sh") != 0:
        return 1
    built = (HERE / "build" / "UBISOUND.IRX").read_bytes()
    if hashlib.sha1(built).hexdigest() != expected:
        print("UBISOUND.IRX: differs from the retail file")
        return 1
    print("UBISOUND.IRX: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
