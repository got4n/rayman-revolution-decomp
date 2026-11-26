#!/usr/bin/env python3
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from elf_match import ElfObject

R_MIPS_26 = 4
R_MIPS_HI16 = 5
R_MIPS_LO16 = 6
R_MIPS_GPREL16 = 7
R_MIPS_LITERAL = 8
FIELD = {
    R_MIPS_26: 0x03FFFFFF,
    R_MIPS_HI16: 0xFFFF,
    R_MIPS_LO16: 0xFFFF,
    R_MIPS_GPREL16: 0xFFFF,
    R_MIPS_LITERAL: 0xFFFF,
}
SHT_SYMTAB = 2
SHT_REL = 9
SHT_NOBITS = 8


def _same_field(a: int, b: int) -> bool:
    gp = {R_MIPS_GPREL16, R_MIPS_LITERAL}
    return a == b or (a in gp and b in gp)


class RawElf:
    def __init__(self, data: bytes):
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            raise ValueError("expected little-endian ELF32")
        self.header = bytearray(data[:52])
        shoff = struct.unpack_from("<I", data, 32)[0]
        shentsize, shnum, self.shstrndx = struct.unpack_from("<HHH", data, 46)
        self.headers = [
            list(struct.unpack_from("<10I", data, shoff + i * shentsize)) for i in range(shnum)
        ]
        self.contents = [
            bytearray() if h[1] == SHT_NOBITS or i == 0 else bytearray(data[h[4]:h[4] + h[5]])
            for i, h in enumerate(self.headers)
        ]

    def name(self, index: int) -> str:
        table = self.contents[self.shstrndx]
        start = self.headers[index][0]
        return table[start:table.index(0, start)].decode()

    def index_of(self, name: str) -> int:
        return next(i for i in range(len(self.headers)) if self.name(i) == name)

    def serialize(self) -> bytes:
        out = bytearray(self.header)
        order = sorted(range(1, len(self.headers)), key=lambda i: self.headers[i][4])
        for index in order:
            header = self.headers[index]
            if header[1] == SHT_NOBITS:
                header[4] = len(out)
                continue
            align = max(header[8], 1)
            out.extend(bytes(-len(out) % align))
            header[4] = len(out)
            header[5] = len(self.contents[index])
            out.extend(self.contents[index])
        out.extend(bytes(-len(out) % 4))
        struct.pack_into("<I", out, 32, len(out))
        for header in self.headers:
            out.extend(struct.pack("<10I", *header))
        return bytes(out)


class Symbols:
    def __init__(self, elf: RawElf):
        self.elf = elf
        self.index = next(i for i, h in enumerate(elf.headers) if h[1] == SHT_SYMTAB)
        self.strtab = elf.headers[self.index][6]
        self.by_name: dict[str, int] = {}
        table = elf.contents[self.index]
        for number in range(len(table) // 16):
            name = self._name(struct.unpack_from("<I", table, number * 16)[0])
            if name:
                self.by_name.setdefault(name, number)

    def _name(self, offset: int) -> str:
        strings = self.elf.contents[self.strtab]
        return strings[offset:strings.index(0, offset)].decode()

    def entry(self, number: int) -> tuple[int, int, int, int, int, int]:
        return struct.unpack_from("<IIIBBH", self.elf.contents[self.index], number * 16)

    def get_or_add(self, name: str) -> int:
        if name in self.by_name:
            return self.by_name[name]
        strings = self.elf.contents[self.strtab]
        offset = len(strings)
        strings.extend(name.encode() + b"\0")
        table = self.elf.contents[self.index]
        number = len(table) // 16
        table.extend(struct.pack("<IIIBBH", offset, 0, 0, 0x10, 0, 0))
        self.by_name[name] = number
        return number


def _text_signature(obj: ElfObject):
    def target(rel):
        return rel.symbol or obj.sections[rel.symbol_section].name
    return obj.text, sorted((r.offset, r.kind, target(r)) for r in obj.relocations)


def _function_relocations(obj: ElfObject, start: int, size: int) -> dict:
    return {r.offset - start: r for r in obj.relocations if start <= r.offset < start + size}


def normalize(target_path: Path, base_path: Path, linked_path: Path) -> tuple[bytes, int]:
    target = ElfObject(target_path)
    base = ElfObject(base_path)
    data = base_path.read_bytes()
    if _text_signature(base) != _text_signature(ElfObject(linked_path)):
        return data, 0

    elf = RawElf(data)
    text = elf.index_of(".text")
    rel_index = next(
        (i for i, h in enumerate(elf.headers) if h[1] == SHT_REL and h[7] == text), None
    )
    if rel_index is None:
        return data, 0
    symbols = Symbols(elf)
    rel_rows = {
        struct.unpack_from("<I", elf.contents[rel_index], row)[0]: row
        for row in range(0, len(elf.contents[rel_index]), 8)
    }
    code = elf.contents[text]
    rewritten = 0

    for name, matches in base.functions.items():
        retail = target.function(name)
        own = base.function(name)
        if retail is None or own is None or retail.size != own.size or not own.size:
            continue
        retail_words = target.function_bytes(retail)
        own_words = base.function_bytes(own)
        retail_rels = _function_relocations(target, retail.value, retail.size)
        own_rels = _function_relocations(base, own.value, own.size)
        clean = True
        for offset in range(0, own.size, 4):
            mask = 0
            for rel in (retail_rels.get(offset), own_rels.get(offset)):
                if rel is not None:
                    mask |= FIELD.get(rel.kind, 0)
            a = struct.unpack_from("<I", retail_words, offset)[0]
            b = struct.unpack_from("<I", own_words, offset)[0]
            if (a ^ b) & ~mask & 0xFFFFFFFF:
                clean = False
                break
        if not clean:
            continue

        for offset, rel in own_rels.items():
            other = retail_rels.get(offset)
            if other is None or not other.symbol or rel.kind not in FIELD:
                continue
            if not _same_field(rel.kind, other.kind):
                continue
            if rel.kind == other.kind and rel.symbol == other.symbol:
                continue
            place = own.value + offset
            word = struct.unpack_from("<I", code, place)[0]
            if other.symbol in symbols.by_name:
                entry = symbols.entry(symbols.by_name[other.symbol])
                if entry[5] != 0:
                    if rel.kind != R_MIPS_26 or entry[5] != text or rel.symbol:
                        continue
                    if entry[1] != (word & FIELD[R_MIPS_26]) << 2:
                        continue
            retail_word = struct.unpack_from("<I", retail_words, offset)[0]
            mask = FIELD[rel.kind]
            struct.pack_into("<I", code, place, (word & ~mask) | (retail_word & mask))
            number = symbols.get_or_add(other.symbol)
            struct.pack_into(
                "<I", elf.contents[rel_index], rel_rows[place] + 4, (number << 8) | other.kind
            )
            rewritten += 1

    if not rewritten:
        return data, 0
    return elf.serialize(), rewritten


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4:
        print("usage: objdiff_normalize.py TARGET BASE LINKED OUT", file=sys.stderr)
        return 2
    target, base, linked, out = (Path(arg) for arg in args)
    data, _ = normalize(target, base, linked)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
