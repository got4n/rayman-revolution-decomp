from __future__ import annotations

import struct
import re
from dataclasses import dataclass
from pathlib import Path


SHT_SYMTAB = 2
SHT_RELA = 4
SHT_NOBITS = 8
SHT_REL = 9
STT_FUNC = 2


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    size: int
    section: int
    kind: int


@dataclass(frozen=True)
class Relocation:
    offset: int
    kind: int
    symbol: str
    addend: int | None
    symbol_section: int
    symbol_value: int
    symbol_kind: int


@dataclass(frozen=True)
class Section:
    name: str
    kind: int
    offset: int
    size: int
    link: int
    info: int
    entsize: int


def _cstring(data: bytes, offset: int) -> str:
    if offset >= len(data):
        raise ValueError("ELF string offset outside table")
    end = data.find(b"\0", offset)
    if end < 0:
        raise ValueError("unterminated ELF string")
    return data[offset:end].decode("utf-8", errors="replace")


class ElfObject:
    def __init__(self, path: Path):
        self.path = path
        data = path.read_bytes()
        if len(data) < 52 or data[:4] != b"\x7fELF" or data[4] != 1:
            raise ValueError(f"{path}: expected ELF32")
        if data[5] not in (1, 2):
            raise ValueError(f"{path}: invalid ELF byte order")
        endian = "<" if data[5] == 1 else ">"
        if struct.unpack_from(endian + "H", data, 18)[0] != 8:
            raise ValueError(f"{path}: expected MIPS ELF")
        shoff = struct.unpack_from(endian + "I", data, 32)[0]
        shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", data, 46)
        if shentsize < 40 or not shnum or shstrndx >= shnum:
            raise ValueError(f"{path}: invalid section table")
        raw_sections = []
        for index in range(shnum):
            offset = shoff + index * shentsize
            if offset + 40 > len(data):
                raise ValueError(f"{path}: truncated section table")
            raw_sections.append(struct.unpack_from(endian + "IIIIIIIIII", data, offset))
        names_header = raw_sections[shstrndx]
        names = data[names_header[4]:names_header[4] + names_header[5]]
        self.sections = [
            Section(_cstring(names, item[0]), item[1], item[4], item[5], item[6], item[7], item[9])
            for item in raw_sections
        ]
        self._data = data
        self._endian = endian
        self._symbols_by_section: dict[int, list[Symbol]] = {}
        for index, section in enumerate(self.sections):
            if section.kind != SHT_SYMTAB:
                continue
            if section.entsize < 16 or section.link >= shnum:
                raise ValueError(f"{path}: invalid symbol table")
            strings = self.section_bytes(section.link)
            symbols = []
            for offset in range(section.offset, section.offset + section.size, section.entsize):
                if offset + 16 > len(data):
                    raise ValueError(f"{path}: truncated symbol table")
                name, value, size, info, _other, shndx = struct.unpack_from(
                    endian + "IIIBBH", data, offset
                )
                symbols.append(Symbol(_cstring(strings, name), value, size, shndx, info & 15))
            self._symbols_by_section[index] = symbols
        if not self._symbols_by_section:
            raise ValueError(f"{path}: missing symbol table")

        self.text_index = next((i for i, s in enumerate(self.sections) if s.name == ".text"), -1)
        if self.text_index < 0:
            raise ValueError(f"{path}: missing .text")
        self.text = self.section_bytes(self.text_index)
        self.functions: dict[str, list[Symbol]] = {}
        for symbols in self._symbols_by_section.values():
            for symbol in symbols:
                if symbol.kind == STT_FUNC and symbol.section == self.text_index:
                    self.functions.setdefault(symbol.name, []).append(symbol)
        self.relocations: list[Relocation] = []
        self.relocations_by_section: dict[int, list[Relocation]] = {}
        for section in self.sections:
            if section.kind not in (SHT_REL, SHT_RELA):
                continue
            symbols = self._symbols_by_section.get(section.link)
            expected_size = 8 if section.kind == SHT_REL else 12
            if symbols is None or section.entsize < expected_size:
                raise ValueError(f"{path}: invalid .text relocation table")
            for offset in range(section.offset, section.offset + section.size, section.entsize):
                if offset + expected_size > len(data):
                    raise ValueError(f"{path}: truncated .text relocations")
                place, info = struct.unpack_from(endian + "II", data, offset)
                symbol_index = info >> 8
                if symbol_index >= len(symbols):
                    raise ValueError(f"{path}: relocation symbol outside table")
                addend = (
                    struct.unpack_from(endian + "i", data, offset + 8)[0]
                    if section.kind == SHT_RELA else None
                )
                target = symbols[symbol_index]
                relocation = Relocation(
                    place, info & 255, target.name, addend,
                    target.section, target.value, target.kind,
                )
                self.relocations_by_section.setdefault(section.info, []).append(relocation)
                if section.info == self.text_index:
                    self.relocations.append(relocation)

    def section_bytes(self, index: int) -> bytes:
        section = self.sections[index]
        if section.kind == SHT_NOBITS:
            return b""
        end = section.offset + section.size
        if end > len(self._data):
            raise ValueError(f"{self.path}: section outside file")
        return self._data[section.offset:end]

    def function(self, name: str) -> Symbol | None:
        matches = self.functions.get(name, [])
        return matches[0] if len(matches) == 1 else None

    def function_bytes(self, symbol: Symbol) -> bytes:
        end = symbol.value + symbol.size
        if not symbol.size or end > len(self.text):
            raise ValueError(f"{self.path}: invalid size for {symbol.name}")
        return self.text[symbol.value:end]

    def function_relocations(self, symbol: Symbol) -> list[tuple[int, int, str, int | None]]:
        start, end = symbol.value, symbol.value + symbol.size
        return sorted(
            (rel.offset - start, rel.kind, rel.symbol, rel.addend)
            for rel in self.relocations if start <= rel.offset < end
        )


def compare_function(retail: ElfObject, base: ElfObject, name: str) -> tuple[str, dict]:
    expected = retail.function(name)
    if expected is None:
        return "missing_retail_symbol", {}
    candidates = base.functions.get(name, [])
    if not candidates:
        return "missing_c_symbol", {"retail_size": expected.size}
    if len(candidates) != 1:
        return "ambiguous_c_symbol", {"candidate_count": len(candidates)}
    actual = candidates[0]
    details = {"retail_size": expected.size, "c_size": actual.size}
    if expected.size != actual.size or not expected.size:
        return "size_mismatch", details
    try:
        a, b = retail.function_bytes(expected), base.function_bytes(actual)
    except ValueError as exc:
        return "invalid_symbol_range", {**details, "error": str(exc)}
    if a != b:
        details["first_byte_difference"] = next(i for i, pair in enumerate(zip(a, b)) if pair[0] != pair[1])
        return "bytes_mismatch", details
    expected_relocs = retail.function_relocations(expected)
    actual_relocs = base.function_relocations(actual)
    details["relocation_count"] = len(expected_relocs)
    if expected_relocs != actual_relocs:
        details["retail_relocations"] = expected_relocs
        details["c_relocations"] = actual_relocs
        return "relocations_mismatch", details
    return "strict_code", details


def symbol_address(name: str, addresses: dict[str, int]) -> int | None:
    if name in addresses:
        return addresses[name]
    suffix = re.fullmatch(r"(.+)_([0-9A-Fa-f]{8})", name)
    if suffix and addresses.get(suffix[1]) == int(suffix[2], 16):
        return int(suffix[2], 16)
    match = re.fullmatch(r"func_([0-9A-Fa-f]{8})", name)
    if match and int(match.group(1), 16) in addresses.values():
        return int(match.group(1), 16)
    if "MAT_fn_" in name:
        return addresses.get(name.replace("MAT_fn_", "POS_fn_"))
    return None


def load_symbol_addresses(path: Path) -> dict[str, int]:
    result = {}
    for name, value in re.findall(
            r"^\s*(\S+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", path.read_text(), re.M):
        address = int(value, 16)
        if name in result and result[name] != address:
            raise ValueError(f"conflicting addresses for {name}")
        result[name] = address
    alias_path = path.with_name("revo.relocation_aliases.json")
    if alias_path.is_file():
        import json
        document = json.loads(alias_path.read_text())
        if document.get("schema_version") != 1:
            raise ValueError("unsupported relocation alias registry")
        for name, row in document["aliases"].items():
            value = int(row["address"], 16)
            if name in result and result[name] != value:
                raise ValueError(f"invalid relocation alias {name}")
            result[name] = value
    for address in set(result.values()):
        result.setdefault(f"func_{address:08X}", address)
    include = path.resolve().parent.parent / "include"
    if include.is_dir():
        for header in include.rglob("*.h"):
            text = header.read_text(errors="replace")
            for alias in re.findall(r"\b[A-Za-z_]\w*_[0-9A-Fa-f]{8}\b", text):
                base, suffix = alias.rsplit("_", 1)
                if base in result and result[base] == int(suffix, 16):
                    result[alias] = result[base]
    return result


def compare_function_resolved(
    retail: ElfObject, base: ElfObject, name: str, addresses: dict[str, int]
) -> tuple[str, dict]:
    left, right = retail.function(name), base.function(name)
    if left is None or right is None or not left.size or left.size != right.size:
        return compare_function(retail, base, name)
    details = {"retail_size": left.size, "c_size": right.size,
               "comparison": "resolved_function_relocations"}
    retail, base = _named_hilo_address_views(retail, base, addresses, left.value - right.value)

    def canonical(obj: ElfObject, function: Symbol):
        contents = bytearray(obj.text[function.value:function.value + function.size])
        if len(contents) != function.size:
            raise ValueError("function outside .text")
        relocations = []
        for relocation in obj.relocations:
            offset = relocation.offset - function.value
            if not 0 <= offset < function.size:
                continue
            target = relocation.symbol
            if (relocation.kind == 4 and relocation.symbol_section == obj.text_index
                    and relocation.symbol_kind == 3):
                if offset + 4 > len(contents):
                    raise ValueError("local call outside function")
                word = struct.unpack_from(obj._endian + "I", contents, offset)[0]
                destination = relocation.symbol_value + ((word & 0x03FFFFFF) << 2)
                matches = [n for n, symbols in obj.functions.items()
                           if len(symbols) == 1 and symbols[0].value == destination]
                if len(matches) != 1:
                    raise ValueError("local call does not target a unique function start")
                target = matches[0]
                struct.pack_into(obj._endian + "I", contents, offset, word & 0xFC000000)
            if (relocation.kind == 7 and relocation.addend is None
                    and relocation.symbol_section == 0):
                address = symbol_address(target, addresses)
                if address is not None:
                    word = struct.unpack_from(obj._endian + "I", contents, offset)[0]
                    immediate = word & 0xFFFF
                    if immediate & 0x8000:
                        immediate -= 0x10000
                    key = f"address:{(address + immediate) & 0xFFFFFFFF:08X}"
                    struct.pack_into(obj._endian + "I", contents, offset, word & 0xFFFF0000)
                    relocations.append((offset, relocation.kind, key, relocation.addend))
                    continue
            address = symbol_address(target, addresses)
            key = f"address:{address:08X}" if address is not None else f"symbol:{target}"
            relocations.append((offset, relocation.kind, key, relocation.addend))
        return bytes(contents), sorted(relocations)

    try:
        a, ar = canonical(retail, left)
        b, br = canonical(base, right)
    except ValueError as exc:
        return "unresolved_relocations", {**details, "error": str(exc)}
    if a != b:
        return "bytes_mismatch", details
    if ar != br:
        return "relocations_mismatch", {**details, "retail_relocations": ar,
                                        "c_relocations": br}
    return "strict_code", {**details, "relocation_count": len(ar)}


def _function_containing(obj: ElfObject, offset: int) -> tuple[str, Symbol] | None:
    matches = []
    for name in obj.functions:
        symbol = obj.function(name)
        if symbol and symbol.value <= offset < symbol.value + symbol.size:
            matches.append((name, symbol))
    return matches[0] if len(matches) == 1 else None


def _named_hilo_address_view(obj: ElfObject, addresses: dict[str, int]):
    import copy
    from dataclasses import replace

    pending, normalized, pairs = {}, {}, {}
    contents = bytearray(obj.text)
    for rel in obj.relocations:
        if rel.symbol_section != 0 or rel.addend is not None:
            continue
        if rel.kind == 5:
            pending.setdefault(rel.symbol, []).append(rel)
            continue
        if rel.kind != 6:
            continue
        highs = pending.pop(rel.symbol, [])
        if len(highs) != 1:
            continue
        high = highs[0]
        address = symbol_address(rel.symbol, addresses)
        owner = _function_containing(obj, high.offset)
        if (address is None or not owner or owner != _function_containing(obj, rel.offset)
                or high.offset >= rel.offset or rel.offset + 4 > len(contents)):
            continue
        hi = struct.unpack_from(obj._endian + "I", obj.text, high.offset)[0]
        lo = struct.unpack_from(obj._endian + "I", obj.text, rel.offset)[0]
        if hi >> 26 != 15 or lo >> 26 not in (9, 25, 32, 33, 35, 36, 37, 40, 41, 43, 49, 55, 57, 63):
            continue
        immediate = lo & 0xFFFF
        if immediate & 0x8000:
            immediate -= 0x10000
        target = (address + ((hi & 0xFFFF) << 16) + immediate) & 0xFFFFFFFF
        key = f"hilo_address:{target:08X}"
        pairs[rel.offset] = high.offset
        for item, word in ((high, hi), (rel, lo)):
            normalized[item.offset] = key
            struct.pack_into(obj._endian + "I", contents, item.offset, word & 0xFFFF0000)
    for offset, key in _dataflow_hilo_keys(obj, addresses, normalized, pairs).items():
        normalized[offset] = key
        word = struct.unpack_from(obj._endian + "I", obj.text, offset)[0]
        struct.pack_into(obj._endian + "I", contents, offset, word & 0xFFFF0000)
    view = copy.copy(obj)
    view.text = bytes(contents)
    view.hilo_pairs = pairs
    view.hilo_linked = {}
    counts = {}
    for rel in obj.relocations:
        counts[rel.offset] = counts.get(rel.offset, 0) + 1
    for index, rel in enumerate(obj.relocations):
        address = symbol_address(rel.symbol, addresses)
        if (rel.kind == 5 and rel.symbol_section == 0 and rel.addend is None
                and address is not None and counts[rel.offset] == 1
                and rel.offset not in normalized and _function_containing(obj, rel.offset)
                and struct.unpack_from(obj._endian + "I", obj.text, rel.offset)[0] >> 26 == 15):
            view.hilo_linked[rel.offset] = _linked_high(obj, index, address)
    view.relocations = [replace(rel, symbol=normalized[rel.offset])
                        if rel.offset in normalized else rel for rel in obj.relocations]
    return view


_HILO_LOW_OPCODES = (9, 25, 32, 33, 35, 36, 37, 40, 41, 43, 49, 55, 57, 63)
_CALL_CLOBBERED = frozenset(range(1, 16)) | {24, 25, 31}


def _signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _register_writes(word: int) -> frozenset[int] | None:
    op, rs, rt, rd, funct = word >> 26, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31, word & 63
    if op == 0:
        if funct in (8, 12, 13, 15, 17, 19, 26, 27) or 0x30 <= funct <= 0x36:
            return frozenset()
        if funct == 9:
            return _CALL_CLOBBERED | {rd}
        return frozenset({rd})
    if op == 1:
        return _CALL_CLOBBERED if 16 <= rt <= 19 else frozenset()
    if op == 3:
        return _CALL_CLOBBERED
    if op in (2, 4, 5, 6, 7, 20, 21, 22, 23):
        return frozenset()
    if 8 <= op <= 15 or op in (24, 25, 26, 27, 30, 55) or 32 <= op <= 39:
        return frozenset({rt})
    if 40 <= op <= 47 or op in (31, 49, 53, 54, 57, 61, 62, 63):
        return frozenset()
    if op in (16, 17, 18):
        return frozenset({rt}) if rs in (0, 1, 2) else frozenset()
    if op == 28:
        return frozenset({rd})
    return None


def _branch_target(word: int, offset: int) -> tuple[int | None, bool] | None:
    op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
    relative = offset + 4 + (_signed16(word) << 2)
    if op in (4, 5, 6, 7):
        return relative, False
    if op in (20, 21, 22, 23):
        return relative, True
    if op == 1 and rt in (0, 1, 2, 3, 16, 17, 18, 19):
        return relative, bool(rt & 2)
    if op in (16, 17, 18) and rs == 8:
        return relative, bool(rt & 2)
    if op == 2:
        return (word & 0x3FFFFFF) << 2, False
    return None


def _linked_high(obj, index: int, address: int) -> int:
    high = obj.relocations[index]
    word = struct.unpack_from(obj._endian + "I", obj.text, high.offset)[0]
    low = 0
    for rel in obj.relocations[index + 1:]:
        if rel.kind == 6 and rel.symbol == high.symbol and rel.symbol_section == 0:
            low = _signed16(struct.unpack_from(obj._endian + "I", obj.text, rel.offset)[0])
            break
    return ((address + ((word & 0xFFFF) << 16) + low + 0x8000) >> 16) & 0xFFFF


def _data_text_targets(obj) -> set[int] | None:
    targets = set()
    for index, relocations in getattr(obj, "relocations_by_section", {}).items():
        if index == obj.text_index:
            continue
        for rel in relocations:
            if rel.symbol_section != obj.text_index:
                continue
            if rel.kind != 2:
                return None
            if rel.addend is None:
                data = obj.section_bytes(index)
                if rel.offset + 4 > len(data):
                    return None
                addend = struct.unpack_from(obj._endian + "I", data, rel.offset)[0]
            else:
                addend = rel.addend
            targets.add((rel.symbol_value + addend) & 0xFFFFFFFF)
    return targets


def _dataflow_hilo_keys(obj, addresses, normalized, pairs):
    endian = obj._endian + "I"
    words = lambda offset: struct.unpack_from(endian, obj.text, offset)[0]
    eligible = lambda rel: (rel.symbol_section == 0 and rel.addend is None
                            and symbol_address(rel.symbol, addresses) is not None)
    high_counts = {}
    for rel in obj.relocations:
        if rel.kind == 5:
            high_counts[rel.offset] = high_counts.get(rel.offset, 0) + 1
    highs = {}
    for index, rel in enumerate(obj.relocations):
        if rel.kind == 5 and eligible(rel) and high_counts[rel.offset] == 1:
            highs.setdefault(rel.symbol, []).append((rel.offset, index))
    keys = {}
    first_use = {}
    indirect = _data_text_targets(obj)
    for rel in obj.relocations:
        if rel.kind != 6 or rel.offset in normalized or not eligible(rel):
            continue
        owner = _function_containing(obj, rel.offset)
        if not owner or rel.offset + 4 > len(obj.text):
            continue
        start, end = owner[1].value, owner[1].value + owner[1].size
        low = words(rel.offset)
        if low >> 26 not in _HILO_LOW_OPCODES:
            continue
        base = (low >> 21) & 31
        candidates = sorted((offset, index) for offset, index in highs.get(rel.symbol, [])
                            if start <= offset < rel.offset)
        if not candidates:
            continue
        chosen = None
        for offset, index in reversed(candidates):
            word = words(offset)
            if word >> 26 == 15 and (word >> 16) & 31 == base:
                chosen = offset, index
                break
        if chosen is None or base == 0:
            continue
        high_offset, high_index = chosen
        before = high_offset - 4
        delay_branch = _branch_target(words(before), before) if before >= start else None
        if delay_branch and delay_branch[1]:
            continue
        safe = True
        for offset in range(start, end, 4):
            word = words(offset)
            if (word >> 26 == 0 and word & 63 == 8 and (word >> 21) & 31 != 31
                    and (indirect is None
                         or any(high_offset < target <= rel.offset for target in indirect))):
                safe = False
                break
            branch = _branch_target(word, offset)
            inside = high_offset < offset < rel.offset
            if (branch and branch[0] is not None and not inside and offset != before
                    and high_offset < branch[0] <= rel.offset):
                safe = False
                break
            if inside:
                written = _register_writes(word)
                if offset == rel.offset - 4 and written == _CALL_CLOBBERED:
                    written = frozenset({31})
                elif offset == rel.offset - 4 and written is not None and word >> 26 == 0:
                    written = frozenset({(word >> 11) & 31}) if word & 63 == 9 else written
                if written is None or base in written:
                    safe = False
                    break
        if not safe:
            continue
        address = symbol_address(rel.symbol, addresses)
        target = ((_linked_high(obj, high_index, address) << 16)
                  + _signed16(address + _signed16(low))) & 0xFFFFFFFF
        keys[rel.offset] = f"hilo_address:{target:08X}"
        pairs[rel.offset] = high_offset
        if high_offset not in normalized:
            first_use.setdefault(high_offset, (rel.offset, target))
            first_use[high_offset] = min(first_use[high_offset], (rel.offset, target))
    for high_offset, (_low, target) in first_use.items():
        keys[high_offset] = f"hilo_address:{target:08X}"
    return keys


def _named_hilo_address_views(retail, base, addresses, offset_delta=0):
    import copy
    from dataclasses import replace
    views = [_named_hilo_address_view(obj, addresses) for obj in (retail, base)]
    def sites(view):
        return {rel.offset for rel in view.relocations if rel.symbol.startswith("hilo_address:")}
    common = sites(views[0]) & {offset + offset_delta for offset in sites(views[1])}
    retail_relocated = {rel.offset for rel in retail.relocations}
    literal = []
    for rel in views[1].relocations:
        offset = rel.offset + offset_delta
        if (not rel.symbol.startswith("hilo_address:") or offset in retail_relocated
                or not 0 <= offset <= len(retail.text) - 4):
            continue
        target = int(rel.symbol.split(":")[1], 16)
        value = ((target + 0x8000) >> 16) & 0xFFFF if rel.kind == 5 else target & 0xFFFF
        built = struct.unpack_from(base._endian + "I", base.text, rel.offset)[0]
        word = struct.unpack_from(retail._endian + "I", retail.text, offset)[0]
        literal.append((replace(rel, offset=offset), word == (built & 0xFFFF0000) | value))
    accepted = {rel.offset for rel, same in literal if same}
    groups = {}
    for low, high in views[1].hilo_pairs.items():
        group = {low + offset_delta, high + offset_delta}
        groups.setdefault(high + offset_delta, set()).update(group)
        groups.setdefault(low + offset_delta, set()).update(group)
    for high_group in list(groups.values()):
        for offset in high_group:
            groups[offset] |= high_group
    literal = [rel for rel, same in literal
               if same and rel.offset in groups and groups[rel.offset] <= accepted]
    common |= {rel.offset for rel in literal}
    linked_high = [{}, {}]
    for side, other, delta in ((0, 1, offset_delta), (1, 0, -offset_delta)):
        for rel in views[other].relocations:
            offset = rel.offset + delta
            if (rel.kind != 5 or not rel.symbol.startswith("hilo_address:")
                    or offset not in views[side].hilo_linked):
                continue
            target = int(rel.symbol.split(":")[1], 16)
            objects = (retail, base)
            mine = struct.unpack_from(objects[side]._endian + "I", objects[side].text, offset)[0]
            theirs = struct.unpack_from(objects[other]._endian + "I", objects[other].text, rel.offset)[0]
            if (mine & 0xFFFF0000 == theirs & 0xFFFF0000
                    and views[side].hilo_linked[offset] == ((target + 0x8000) >> 16) & 0xFFFF):
                linked_high[side][offset] = rel.symbol
    for rel in base.relocations:
        offset = rel.offset + offset_delta
        value = views[1].hilo_linked.get(rel.offset)
        if (rel.kind != 5 or value is None or offset in retail_relocated
                or rel.offset in linked_high[1] or not 0 <= offset <= len(retail.text) - 4):
            continue
        built = struct.unpack_from(base._endian + "I", base.text, rel.offset)[0]
        word = struct.unpack_from(retail._endian + "I", retail.text, offset)[0]
        if word == (built & 0xFFFF0000) | value:
            key = f"hilo_high:{value:04X}"
            linked_high[1][rel.offset] = key
            literal.append(replace(rel, offset=offset, symbol=key))
    common |= set(linked_high[0]) | {offset + offset_delta for offset in linked_high[1]}
    output = []
    for index, (obj, view) in enumerate(zip((retail, base), views)):
        selected_sites = common if index == 0 else {offset - offset_delta for offset in common}
        text = bytearray(obj.text)
        for offset in selected_sites:
            text[offset:offset + 4] = view.text[offset:offset + 4]
        selected = copy.copy(view)
        selected.text = bytes(text)
        selected.relocations = [new if old.offset in selected_sites else old
                                for old, new in zip(obj.relocations, view.relocations)]
        if linked_high[index]:
            for offset in linked_high[index]:
                word = struct.unpack_from(obj._endian + "I", text, offset)[0]
                struct.pack_into(obj._endian + "I", text, offset, word & 0xFFFF0000)
            selected.text = bytes(text)
            selected.relocations = [replace(rel, symbol=linked_high[index][rel.offset])
                                    if rel.offset in linked_high[index] else rel
                                    for rel in selected.relocations]
        if index == 0:
            for rel in literal:
                word = struct.unpack_from(obj._endian + "I", text, rel.offset)[0]
                struct.pack_into(obj._endian + "I", text, rel.offset, word & 0xFFFF0000)
            selected.text = bytes(text)
            selected.relocations = selected.relocations + literal
        output.append(selected)
    return tuple(output)


def _paired_hilo_target(obj: ElfObject, high: Relocation, low: Relocation) -> int | None:
    high_word = struct.unpack_from(obj._endian + "I", obj.text, high.offset)[0]
    low_word = struct.unpack_from(obj._endian + "I", obj.text, low.offset)[0]
    if high_word >> 26 != 0x0F:
        return None

    low_immediate = low_word & 0xFFFF
    if low_word >> 26 != 0x0D and low_immediate & 0x8000:
        low_immediate -= 0x10000
    target = high.symbol_value + ((high_word & 0xFFFF) << 16) + low_immediate
    return target & 0xFFFFFFFF


def _local_hilo_function_relocations(
    retail: ElfObject, base: ElfObject, names: set[str]
) -> dict[int, str]:
    retail_at: dict[tuple[int, int], list[Relocation]] = {}
    for relocation in retail.relocations:
        retail_at.setdefault((relocation.offset, relocation.kind), []).append(relocation)

    local = [
        relocation for relocation in base.relocations
        if relocation.kind in (5, 6)
        and relocation.symbol_section == base.text_index
        and relocation.symbol_kind == 3
    ]
    normalized: dict[int, str] = {}
    for relocation in local:
        expected_relocations = retail_at.get((relocation.offset, relocation.kind), [])
        if len(expected_relocations) != 1:
            continue
        expected = expected_relocations[0]
        if (
            expected.symbol_section != retail.text_index
            or expected.symbol_kind != STT_FUNC
            or expected.symbol not in names
        ):
            continue
        expected_symbol = retail.function(expected.symbol)
        owner = _function_containing(base, relocation.offset)
        if not expected_symbol or expected_symbol.value != expected.symbol_value or not owner:
            continue

        if relocation.kind == 5:
            high = relocation
            high_word = struct.unpack_from(base._endian + "I", base.text, high.offset)[0]
            if high_word >> 26 != 0x0F:
                continue
            candidates = []
            for low in local:
                if (
                    low.kind != 6 or low.offset <= high.offset
                    or low.symbol_section != high.symbol_section
                    or low.symbol_value != high.symbol_value
                    or _function_containing(base, low.offset) != owner
                ):
                    continue
                low_expected = retail_at.get((low.offset, low.kind), [])
                if (
                    len(low_expected) == 1
                    and low_expected[0].symbol == expected.symbol
                    and low_expected[0].symbol_value == expected.symbol_value
                    and _paired_hilo_target(base, high, low) == expected_symbol.value
                ):
                    candidates.append(low)
            if not candidates:
                continue
            companion = min(candidates, key=lambda item: item.offset)
        else:
            low = relocation
            candidates = []
            for high in local:
                if (
                    high.kind != 5 or high.offset >= low.offset
                    or high.symbol_section != low.symbol_section
                    or high.symbol_value != low.symbol_value
                    or _function_containing(base, high.offset) != owner
                ):
                    continue
                high_expected = retail_at.get((high.offset, high.kind), [])
                if (
                    len(high_expected) == 1
                    and high_expected[0].symbol == expected.symbol
                    and high_expected[0].symbol_value == expected.symbol_value
                    and _paired_hilo_target(base, high, low) == expected_symbol.value
                ):
                    candidates.append(high)
            if not candidates:
                continue
            companion = max(candidates, key=lambda item: item.offset)

        companion_expected = retail_at.get((companion.offset, companion.kind), [])
        if (
            len(companion_expected) == 1
            and companion_expected[0].symbol == expected.symbol
            and companion_expected[0].symbol_value == expected.symbol_value
        ):
            normalized[relocation.offset] = expected.symbol
    return normalized


def _literal_lo16_view(retail, base, addresses):
    import copy
    from dataclasses import replace
    relocations = list(retail.relocations)
    occupied = {r.offset for r in relocations}
    contents = bytearray(retail.text)
    for low in base.relocations:
        if low.kind != 6 or low.addend is not None or low.symbol_section != 0 or low.offset in occupied:
            continue
        address = symbol_address(low.symbol, addresses)
        if address is None or low.offset + 4 > len(contents):
            continue
        word = struct.unpack_from(retail._endian + "I", contents, low.offset)[0]
        actual = struct.unpack_from(base._endian + "I", base.text, low.offset)[0]
        if word >> 26 not in (0x20, 0x21, 0x23, 0x24, 0x25, 0x28, 0x29, 0x2B, 0x31, 0x39):
            continue
        if word & 0xFFFF0000 != actual & 0xFFFF0000 or word & 0xFFFF != address & 0xFFFF or actual & 0xFFFF:
            continue
        register = (word >> 21) & 31
        owner = _function_containing(retail, low.offset)
        if not owner:
            continue
        for high in reversed(sorted(retail.relocations, key=lambda r: r.offset)):
            same_target = high.symbol == low.symbol or (
                high.symbol_section == 0 and high.addend is None
                and symbol_address(high.symbol, addresses) == address
            )
            if high.kind != 5 or not same_target or high.offset >= low.offset or _function_containing(retail, high.offset) != owner:
                continue
            hw = struct.unpack_from(retail._endian + "I", contents, high.offset)[0]
            if hw >> 26 != 15 or (hw >> 16) & 31 != register:
                continue
            safe = True
            for offset in range(high.offset + 4, low.offset, 4):
                instruction = struct.unpack_from(retail._endian + "I", contents, offset)[0]
                opcode, rt = instruction >> 26, (instruction >> 16) & 31
                if opcode in (8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 26, 27, 32, 33, 34, 35, 36, 37, 38, 55):
                    safe = rt != register
                elif opcode in (40, 41, 42, 43, 44, 45, 46, 49, 53, 57, 61, 63):
                    safe = True
                elif instruction == 0:
                    safe = True
                else:
                    written = _register_writes(instruction)
                    control = (_branch_target(instruction, offset) is not None or opcode in (1, 3)
                               or (opcode == 0 and instruction & 63 in (8, 9, 12, 13)))
                    safe = written is not None and not control and register not in written
                if not safe:
                    break
            if safe:
                relocations.append(replace(low, symbol=high.symbol))
                struct.pack_into(retail._endian + "I", contents, low.offset, word & 0xFFFF0000)
            break
    view = copy.copy(retail)
    view.text, view.relocations = bytes(contents), relocations
    return view


def compare_text_layout(
    retail: ElfObject, base: ElfObject, names: list[str],
    addresses: dict[str, int] | None = None,
) -> tuple[bool, str]:
    for name in names:
        left, right = retail.function(name), base.function(name)
        if left is None or right is None or (left.value, left.size) != (right.value, right.size):
            return False, f"function layout differs: {name}"
    if len(retail.text) != len(base.text):
        shorter, longer = sorted((retail.text, base.text), key=len)
        last_end = max(
            (retail.function(name).value + retail.function(name).size for name in names),
            default=0,
        )
        if (
            len(longer) - len(shorter) > 15
            or len(shorter) != last_end
            or any(longer[len(shorter):])
        ):
            return False, "text size differs beyond zero tail padding"

    retail = _literal_lo16_view(retail, base, addresses or {})
    retail, base = _named_hilo_address_views(retail, base, addresses or {})

    hilo_symbols = _local_hilo_function_relocations(retail, base, set(names))
    pc16_targets = {
        relocation.offset: relocation.symbol
        for relocation in retail.relocations
        if relocation.kind == 10
        and relocation.symbol in names
        and relocation.symbol_section == retail.text_index
        and relocation.symbol_kind == STT_FUNC
        and retail.function(relocation.symbol) is not None
        and retail.function(relocation.symbol).value == relocation.symbol_value
    }

    def canonical(obj: ElfObject) -> tuple[bytes, list[tuple[int, int, str, int | None]]]:
        contents = bytearray(obj.text)
        output = []
        endian = "<" if obj._endian == "<" else ">"
        seen_relocations = set()
        for rel in obj.relocations:
            symbol = rel.symbol
            if rel.kind == 4:
                if rel.offset + 4 > len(contents):
                    raise ValueError("R_MIPS_26 outside .text")
                word = struct.unpack_from(endian + "I", contents, rel.offset)[0]
                if rel.symbol_section == obj.text_index and rel.symbol_kind == 3:
                    target_offset = (rel.symbol_value + ((word & 0x03FFFFFF) << 2)) & 0xFFFFFFFF
                    matches = [
                        name for name, symbols in obj.functions.items()
                        if len(symbols) == 1 and symbols[0].value == target_offset
                    ]
                    if len(matches) != 1:
                        raise ValueError(f"unresolved local R_MIPS_26 at {rel.offset:#x}")
                    symbol = matches[0]
                    struct.pack_into(endian + "I", contents, rel.offset, word & 0xFC000000)
            elif rel.kind == 7 and rel.addend is None:
                address = symbol_address(symbol, addresses or {})
                if address is not None and rel.symbol_section == 0:
                    word = struct.unpack_from(endian + "I", contents, rel.offset)[0]
                    immediate = word & 0xFFFF
                    if immediate & 0x8000:
                        immediate -= 0x10000
                    symbol = f"address:{(address + immediate) & 0xFFFFFFFF:08X}"
                    struct.pack_into(endian + "I", contents, rel.offset, word & 0xFFFF0000)
            elif rel.kind in (5, 6) and rel.offset in hilo_symbols:
                target = hilo_symbols[rel.offset]
                if obj is base and rel.symbol_section == obj.text_index and rel.symbol_kind == 3:
                    symbol = target
                elif obj is retail and rel.symbol == target:
                    symbol = target
                else:
                    output.append((rel.offset, rel.kind, symbol, rel.addend))
                    continue
                word = struct.unpack_from(endian + "I", contents, rel.offset)[0]
                struct.pack_into(endian + "I", contents, rel.offset, word & 0xFFFF0000)
            elif rel.kind == 10 and rel.offset in pc16_targets:
                symbol = pc16_targets[rel.offset]
                if obj is base:
                    word = struct.unpack_from(endian + "I", contents, rel.offset)[0]
                    branch_target = (
                        rel.offset + 4 + ((word & 0xFFFF) - (0x10000 if word & 0x8000 else 0)) * 4
                    ) & 0xFFFFFFFF
                    target = base.function(symbol)
                    if target is None or branch_target != target.value:
                        output.append((rel.offset, rel.kind, rel.symbol, rel.addend))
                        continue
                word = struct.unpack_from(endian + "I", contents, rel.offset)[0]
                struct.pack_into(endian + "I", contents, rel.offset, word & 0xFFFF0000)
                seen_relocations.add(rel.offset)
            address = symbol_address(symbol, addresses or {})
            if address is not None:
                symbol = f"address:{address:08X}"
            output.append((rel.offset, rel.kind, symbol, rel.addend))
        if obj is base:
            for offset, symbol in pc16_targets.items():
                if offset in seen_relocations or any(rel.offset == offset for rel in obj.relocations):
                    continue
                if offset + 4 > len(contents):
                    continue
                word = struct.unpack_from(endian + "I", contents, offset)[0]
                branch_target = (
                    offset + 4 + ((word & 0xFFFF) - (0x10000 if word & 0x8000 else 0)) * 4
                ) & 0xFFFFFFFF
                target = base.function(symbol)
                if target is None or branch_target != target.value:
                    continue
                struct.pack_into(endian + "I", contents, offset, word & 0xFFFF0000)
                address = symbol_address(symbol, addresses or {})
                if address is not None:
                    symbol = f"address:{address:08X}"
                output.append((offset, 10, symbol, None))
        return bytes(contents).ljust(max(len(retail.text), len(base.text)), b"\0"), sorted(output)

    try:
        left, left_relocs = canonical(retail)
        right, right_relocs = canonical(base)
    except ValueError as exc:
        return False, str(exc)
    if left != right:
        return False, "text bytes differ after same-TU relocation normalization"
    if left_relocs != right_relocs:
        return False, "text relocations differ after same-TU relocation normalization"
    return True, "text bytes, function layout and text relocations match"
