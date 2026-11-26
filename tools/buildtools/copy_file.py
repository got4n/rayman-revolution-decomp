#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: copy_file.py SOURCE DESTINATION", file=sys.stderr)
        return 2
    source, destination = Path(args[0]), Path(args[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
