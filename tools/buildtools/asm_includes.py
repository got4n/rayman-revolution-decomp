from __future__ import annotations

import re
from pathlib import Path


INCLUDE = re.compile(
    r'\bINCLUDE_ASM\(\s*"([^"]+)"\s*,\s*([A-Za-z_]\w*)\s*\)'
)


def included_asm_files(root: Path, source: Path) -> list[str]:
    text = source.read_text(encoding="utf-8")
    found: set[str] = set()
    for folder, symbol in INCLUDE.findall(text):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", folder):
            raise ValueError(f"invalid INCLUDE_ASM path in {source}: {folder}")
        rel = Path(folder) / f"{symbol}.s"
        if (
            rel.is_absolute()
            or ".." in rel.parts
            or rel.parts[:2] != ("asm", "nonmatchings")
        ):
            raise ValueError(f"invalid INCLUDE_ASM path in {source}: {rel}")
        if not (root / rel).is_file():
            raise FileNotFoundError(f"missing INCLUDE_ASM fragment: {rel}")
        found.add(rel.as_posix())
    return sorted(found)
