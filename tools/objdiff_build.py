#!/usr/bin/env python3
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ninja = shutil.which("ninja")
    if ninja is None:
        print("Ninja is missing. Install ninja and follow docs/build.md.", file=sys.stderr)
        return 1
    if not (root / "build.ninja").is_file():
        result = subprocess.run(
            [sys.executable, "configure.py", "--noclean"], cwd=root
        )
        if result.returncode:
            print("Build setup failed. Follow docs/build.md for the retail input, "
                  "Python dependencies and external tools.", file=sys.stderr)
            return result.returncode
    return subprocess.run([ninja, *sys.argv[1:]], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
