#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HEX = set("0123456789abcdef")
ELF_MAGIC = b"\x7fELF"
PT_LOAD = 1
SHT_PROGBITS = 1


def parse_checksums(path: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"{path}:{number}: expected '<sha1>  <file>'")
        digest, name = parts[0].lower(), parts[1].strip()
        if name.startswith("*"):
            name = name[1:]
        if len(digest) != 40 or any(char not in HEX for char in digest):
            raise ValueError(f"{path}:{number}: not a sha1 digest: {digest!r}")
        entries.append((digest, Path(name)))
    if not entries:
        raise ValueError(f"{path}: no checksum line found")
    return entries


def resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _loads(data: bytes) -> list[tuple[int, int, int]]:
    if len(data) < 52 or data[:4] != ELF_MAGIC or data[4] != 1:
        raise ValueError("retail input is not an ELF32 file")
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize = struct.unpack_from("<H", data, 42)[0]
    e_phnum = struct.unpack_from("<H", data, 44)[0]
    loads: list[tuple[int, int, int]] = []
    for index in range(e_phnum):
        off = e_phoff + index * e_phentsize
        p_type, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from("<IIIII", data, off)
        if p_type == PT_LOAD and p_filesz:
            loads.append((p_paddr, p_offset, p_filesz))
    if not loads:
        raise ValueError("retail input has no PT_LOAD segment")
    return loads


def retail_image(data: bytes) -> tuple[int, bytes]:
    loads = _loads(data)
    base = min(paddr for paddr, _, _ in loads)
    end = max(paddr + size for paddr, _, size in loads)
    image = bytearray(end - base)
    for paddr, offset, size in loads:
        image[paddr - base : paddr - base + size] = data[offset : offset + size]
    return base, bytes(image)


def section_name(data: bytes, address: int) -> str:
    if len(data) < 52:
        return "?"
    e_shoff = struct.unpack_from("<I", data, 32)[0]
    e_shentsize = struct.unpack_from("<H", data, 46)[0]
    e_shnum = struct.unpack_from("<H", data, 48)[0]
    e_shstrndx = struct.unpack_from("<H", data, 50)[0]
    if not e_shoff or e_shstrndx >= e_shnum:
        return "?"
    names_off = e_shoff + e_shstrndx * e_shentsize
    sh_offset = struct.unpack_from("<I", data, names_off + 16)[0]
    sh_size = struct.unpack_from("<I", data, names_off + 20)[0]
    names = data[sh_offset : sh_offset + sh_size]
    for index in range(e_shnum):
        off = e_shoff + index * e_shentsize
        sh_name, sh_type, _flags, sh_addr, _sh_offset, sh_size = struct.unpack_from(
            "<IIIIII", data, off
        )
        if sh_type != SHT_PROGBITS or not sh_size:
            continue
        if sh_addr <= address < sh_addr + sh_size:
            end = names.find(b"\0", sh_name)
            return names[sh_name:end].decode("ascii", "replace") or "?"
    return "?"


def compare_rom(elf: bytes, rom: bytes) -> str | None:
    base, image = retail_image(elf)
    if len(rom) != len(image):
        return (
            f"ROM length {len(rom)} != PT_LOAD span {len(image)} "
            f"starting at 0x{base:08X}"
        )
    for index, (got, expected) in enumerate(zip(rom, image)):
        if got != expected:
            address = base + index
            return (
                f"first difference at 0x{address:08X} section {section_name(elf, address)} "
                f"(rom 0x{got:02X}, retail 0x{expected:02X})"
            )
    return None


def drop_marker(marker: Path) -> None:
    if marker.is_file():
        marker.unlink()


def marker_text(retail: Path, rom: Path, checksum_file: Path) -> str:
    checker = Path(__file__).resolve()
    lines = [
        f"input_sha1 {sha1_file(retail)} {retail.relative_to(ROOT).as_posix()}",
        f"rom_sha256 {sha256_file(rom)} {rom.relative_to(ROOT).as_posix()}",
        f"checksum_sha256 {sha256_file(checksum_file)} {checksum_file.relative_to(ROOT).as_posix()}",
        f"checker_sha256 {sha256_file(checker)} {checker.relative_to(ROOT).as_posix()}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--self-test"]:
        return self_test()
    if len(args) != 3:
        print("usage: check_sha1.py CHECKSUM_FILE ROM MARKER | --self-test", file=sys.stderr)
        return 2

    checksum_file, rom_path, marker = (resolve(Path(arg)) for arg in args)
    try:
        entries = parse_checksums(checksum_file)
    except (OSError, ValueError) as exc:
        print(f"check_sha1: {exc}", file=sys.stderr)
        drop_marker(marker)
        return 2

    failures: list[str] = []
    retail: Path | None = None
    for expected, name in entries:
        path = resolve(name)
        if not path.is_file():
            failures.append(f"input {name}: missing")
            continue
        actual = sha1_file(path)
        if actual == expected:
            print(f"input {name}: OK")
            retail = path
        else:
            failures.append(f"input {name}: expected {expected}, got {actual}")

    if failures or retail is None:
        for problem in failures:
            print(f"check_sha1: {problem}", file=sys.stderr)
        drop_marker(marker)
        return 1

    if not rom_path.is_file():
        print(f"check_sha1: ROM missing: {rom_path}", file=sys.stderr)
        drop_marker(marker)
        return 1

    elf = retail.read_bytes()
    rom = rom_path.read_bytes()
    try:
        mismatch = compare_rom(elf, rom)
    except ValueError as exc:
        print(f"check_sha1: {exc}", file=sys.stderr)
        drop_marker(marker)
        return 1
    if mismatch:
        print(f"check_sha1: {mismatch}", file=sys.stderr)
        drop_marker(marker)
        return 1

    print(f"ROM {rom_path.name}: PT_LOAD OK ({len(rom)} bytes)")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(marker_text(retail, rom_path, checksum_file), encoding="utf-8", newline="\n")
    print(f"{marker}: OK")
    return 0


def self_test() -> int:
    retail = ROOT / "iso" / "SLUS_201.38"
    rom = ROOT / "build" / "SLUS_201.38.rom"
    if not retail.is_file() or not rom.is_file():
        print("check_sha1 self-test: non vérifié (iso or ROM missing)")
        return 0
    elf = retail.read_bytes()
    original = rom.read_bytes()
    if compare_rom(elf, original) is not None:
        print("check_sha1 self-test: the current ROM already differs from retail", file=sys.stderr)
        return 1
    flipped = bytearray(original)
    flipped[0] ^= 0xFF
    mismatch = compare_rom(elf, bytes(flipped))
    if mismatch is None or "first difference" not in mismatch:
        print(f"check_sha1 self-test: altered ROM was accepted ({mismatch})", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "rom.ok"
        marker.write_text("stale\n", encoding="utf-8")
        bad = Path(tmp) / "bad.rom"
        bad.write_bytes(bytes(flipped))
        code = main([
            "config/revo.checksum.sha1",
            str(bad),
            str(marker),
        ])
        if code == 0 or marker.exists():
            print("check_sha1 self-test: failure left a marker", file=sys.stderr)
            return 1
    print(f"check_sha1 self-test: OK ({mismatch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
