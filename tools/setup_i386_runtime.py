#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=ROOT / ".i386-root")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        raise SystemExit("destination must not exist; existing runtime is preserved")
    if shutil.which("docker") is None:
        raise SystemExit("Docker is required for this runtime preparation")
    destination.mkdir(parents=True)
    script = r"""
set -eu
dpkg --add-architecture i386
apt-get update
apt-get install -y --no-install-recommends libc6:i386 libstdc++6:i386 qemu-user-static
mkdir -p /output/lib /output/usr/lib /output/usr/bin
cp -R /usr/lib/i386-linux-gnu /output/usr/lib/
cp -L /lib/ld-linux.so.2 /output/lib/ld-linux.so.2
cp /usr/bin/qemu-i386-static /output/usr/bin/
dpkg-query -W libc6:i386 libstdc++6:i386 libgcc-s1:i386 qemu-user-static > /output/packages.txt
"""
    code = subprocess.run([
        "docker", "run", "--platform", "linux/amd64", "--rm",
        "--mount", f"type=bind,source={destination},target=/output",
        "debian:bookworm-slim", "bash", "-c", script,
    ]).returncode
    if code:
        print("Runtime preparation failed; preserve the output for diagnosis.")
        return code
    print(f"OK: runtime prepared in {destination.name}; run python3 configure.py --check next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
