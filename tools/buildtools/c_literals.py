from __future__ import annotations

import copy
import json
import re
import struct
from pathlib import Path

from c_rodata import retail_data, FILE_DELTA
from elf_match import load_symbol_addresses, symbol_address

REGISTRY = 'config/revo.c_literals.json'
START, END = 0x392700, 0x393200


def registrations(root):
    path = root / REGISTRY
    if not path.is_file():
        return []
    document = json.loads(path.read_text())
    if document.get('schema_version') != 1:
        raise ValueError('unsupported C literal registry')
    entries = sorted(document['ranges'], key=lambda row: int(row['start'], 16))
    cursor, seen = START, set()
    for row in entries:
        start, size = int(row['start'], 16), row['size']
        if (row['tu'] in seen or start < cursor or size <= 0 or size % 4
                or start % 4 or start + size > END):
            raise ValueError('overlapping, duplicate or invalid C literal registration')
        seen.add(row['tu'])
        cursor = start + size
    return entries


def retained_layout(root, splits, linked):
    entries = registrations(root)
    if not entries:
        return [], []
    rows = {row['splat_path']: row for row in splits}
    full = (root / 'asm/data/main/lit4.rodata.s').read_text()
    symbols, pending = [], None
    for line in full.splitlines():
        label = re.match(r'\s*dlabel\s+(\w+)', line)
        if label:
            pending = label[1]
        address = re.search(r'/\*\s*[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s+', line)
        if pending and address:
            symbols += [f'.globl {pending}', f'.set {pending}, 0x{address[1]}']
            pending = None
    paths, sources, cursor = [], [], START
    folder = root / 'asm/data/linked_literals'
    folder.mkdir(parents=True, exist_ok=True)

    def gap(end):
        nonlocal cursor
        if end == cursor:
            return
        path = folder / f'retained_{cursor:08X}.s'
        text = '.section .lit4, "a"\n.balign 1\n'
        if not sources:
            text += '\n'.join(symbols) + '\n'
        text += f'.incbin "iso/SLUS_201.38", {cursor - FILE_DELTA}, {end - cursor}\n'
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
        source = path.relative_to(root).as_posix()
        paths.append(source + '.o')
        sources.append(source)
        cursor = end

    for row in entries:
        tu = rows.get(row['tu'])
        if not tu or tu['decomp_scope'] not in {'project', 'unresolved'}:
            raise ValueError('C literals must belong to an allowed text split')
        offset = int(tu['start'], 16) - FILE_DELTA
        if offset not in linked:
            raise ValueError('C literals belong to an ASM-linked split')
        retail_data(root, row)
        gap(int(row['start'], 16))
        paths.append(linked[offset])
        cursor += row['size']
    gap(END)
    return paths, sources


def verified_view(root, row, retail, built):
    index = next((i for i, section in enumerate(built.sections) if section.name == '.lit4'), -1)
    if index < 0 or built.sections[index].size != row['size']:
        raise ValueError('registered compiled .lit4 size differs')
    if built.section_bytes(index) != retail_data(root, row):
        raise ValueError('compiled float literals differ from retail bytes')
    if built.relocations_by_section.get(index):
        raise ValueError('float literals must contain no relocations')
    addresses = load_symbol_addresses(root / 'config/revo.symbol_addrs.txt')
    literal_asm = root / 'asm/data/main/lit4.rodata.s'
    pending = None
    if literal_asm.is_file():
        for line in literal_asm.read_text().splitlines():
            label = re.match(r'\s*dlabel\s+(\w+)', line)
            if label:
                pending = label[1]
            location = re.search(r'/\*\s*[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s+', line)
            if pending and location:
                address = int(location[1], 16)
                if pending in addresses and addresses[pending] != address:
                    raise ValueError('literal symbol address conflicts with retail data')
                addresses[pending] = address
                pending = None
    expected = {r.offset: r for r in retail.relocations}
    local = [r for r in built.relocations if r.symbol_section == index]
    if not local or len({r.offset for r in local}) != len(local):
        raise ValueError('literal text references must be nonempty and unique')
    contents = bytearray(built.text)
    replacements = {}
    for actual in local:
        if (actual.kind != 8 or actual.symbol_kind != 3 or actual.addend is not None
                or actual.offset % 4 or not 0 <= actual.offset <= len(contents) - 4):
            raise ValueError('unsupported text reference to C float literals')
        word = struct.unpack_from(built._endian + 'I', contents, actual.offset)[0]
        relative = actual.symbol_value + (((word & 0xFFFF) + 0x4000) & 0xFFFF)
        reference = expected.get(actual.offset)
        if not reference or reference.kind != 7 or reference.addend is not None:
            raise ValueError('literal reference lacks a retail GP relocation')
        address = symbol_address(reference.symbol, addresses)
        target_word = struct.unpack_from(retail._endian + 'I', retail.text, actual.offset)[0]
        immediate = target_word & 0xFFFF
        if immediate & 0x8000:
            immediate -= 0x10000
        if (address is None or relative % 4 or not 0 <= relative <= row['size'] - 4
                or int(row['start'], 16) + relative != address + immediate):
            raise ValueError('literal reference lacks a proven retail address')
        struct.pack_into(built._endian + 'I', contents, actual.offset,
                         (word & 0xFFFF0000) | (target_word & 0xFFFF))
        replacements[actual.offset] = reference
    view = copy.copy(built)
    view.text = bytes(contents)
    view.relocations = [replacements.get(r.offset, r) for r in built.relocations]
    return view
