from __future__ import annotations

import copy
import hashlib
import json
import re
import struct
from dataclasses import replace

from elf_match import ElfObject, load_symbol_addresses, _paired_hilo_target

REGISTRY = 'config/revo.c_rodata.json'
START, END = 0x388D80, 0x392580
FILE_DELTA = 0xFF000
DATA_LAYOUTS = {
    '.rodata': (START, END, 'rodata.rodata.s', 'linked_rodata'),
    '.sdata': (0x393200, 0x393EF8, 'sdata.data.s', 'linked_sdata'),
}


def registrations(root, section='.rodata'):
    path = root / REGISTRY
    if not path.is_file():
        return []
    document = json.loads(path.read_text())
    if document.get('schema_version') != 1:
        raise ValueError('unsupported C rodata registry')
    if section not in DATA_LAYOUTS or any(
            row.get('section', '.rodata') not in DATA_LAYOUTS
            or row.get('kind', 'jump_table') not in {'jump_table', 'strings'}
            or (row.get('section') == '.sdata' and row.get('kind') != 'strings')
            for row in document['ranges']):
        raise ValueError('unsupported C data section or kind')
    entries = sorted((row for row in document['ranges']
                      if row.get('section', '.rodata') == section),
                     key=lambda row: int(row['start'], 16))
    begin, end, _, _ = DATA_LAYOUTS[section]
    cursor, seen = begin, set()
    for row in entries:
        start, size = int(row['start'], 16), row['size']
        if row['tu'] in seen or start < cursor or size <= 0 or size % 4 or start % 4 or start + size > end:
            raise ValueError('overlapping, duplicate or invalid C rodata registration')
        if row.get('kind') == 'strings' and not size - 3 <= row.get('compiled_size', 0) <= size:
            raise ValueError('invalid compiled string size or trailing alignment')
        if row.get('kind', 'jump_table') == 'jump_table' and 'compiled_size' in row \
                and not size - 3 <= row['compiled_size'] < size:
            raise ValueError('invalid compiled table size or trailing alignment')
        seen.add(row['tu'])
        cursor = start + size
    return entries


def retail_data(root, row):
    start, size = int(row['start'], 16), row['size']
    with (root / 'iso/SLUS_201.38').open('rb') as handle:
        handle.seek(start - FILE_DELTA)
        data = handle.read(size)
    if len(data) != size or hashlib.sha256(data).hexdigest() != row['retail_sha256']:
        raise ValueError('registered retail jump table fingerprint differs')
    return data


def retained_layout(root, splits, linked, section='.rodata'):
    entries = registrations(root, section)
    begin, end, filename, generated_folder = DATA_LAYOUTS[section]
    if not entries:
        return [f'asm/data/main/{filename}.o'], []
    rows = {row['splat_path']: row for row in splits}
    full = (root / 'asm/data/main' / filename).read_text()
    symbols, pending = [], None
    for line in full.splitlines():
        label = re.match(r'\s*dlabel\s+(\w+)', line)
        if label:
            pending = label[1]
        address = re.search(r'/\*\s*[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s+', line)
        if pending and address:
            symbols += [f'.globl {pending}', f'.set {pending}, 0x{address[1]}']
            pending = None
    paths, generated, cursor = [], [], begin
    folder = root / 'asm/data' / generated_folder
    folder.mkdir(parents=True, exist_ok=True)

    def gap(end):
        nonlocal cursor
        if end == cursor:
            return
        path = folder / f'retained_{cursor:08X}.s'
        flags = 'aw' if section == '.sdata' else 'a'
        text = f'.section {section}, "{flags}"\n.balign 1\n'
        if not generated:
            text += '\n'.join(symbols) + '\n'
        text += f'.incbin "iso/SLUS_201.38", {cursor - FILE_DELTA}, {end - cursor}\n'
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
        source = path.relative_to(root).as_posix()
        paths.append(source + '.o')
        generated.append(source)
        cursor = end

    for row in entries:
        tu = rows.get(row['tu'])
        if not tu or tu['decomp_scope'] not in {'project', 'unresolved'}:
            raise ValueError('C rodata range must belong to an allowed text split')
        offset = int(tu['start'], 16) - FILE_DELTA
        if offset not in linked:
            raise ValueError('C rodata range belongs to an ASM-linked split')
        retail_data(root, row)
        gap(int(row['start'], 16))
        paths.append(linked[offset])
        cursor += row['size']
    gap(end)
    return paths, generated


def verified_view(root, row, retail, built, text_start):
    section_name = row.get('section', '.rodata')
    strings = row.get('kind') == 'strings'
    compiled_size = row.get('compiled_size', row['size'])
    index = next((i for i, section in enumerate(built.sections) if section.name == section_name), -1)
    if index < 0 or built.sections[index].size != compiled_size:
        raise ValueError(f'registered compiled {section_name} size differs')
    data = bytearray(built.section_bytes(index))
    relocations = built.relocations_by_section.get(index, [])
    if strings and (relocations or not row['size'] - 3 <= compiled_size <= row['size']):
        raise ValueError('strings require no data relocations and at most 3 alignment bytes')
    if not strings and (not relocations or len(relocations) != len({r.offset for r in relocations})):
        raise ValueError('jump table relocations must be unique')
    for relocation in relocations:
        if relocation.offset % 4 or not 0 <= relocation.offset <= len(data) - 4:
            raise ValueError('jump table relocation is not a word in the range')
        if relocation.kind != 2 or relocation.symbol_section != built.text_index or relocation.addend is not None:
            raise ValueError('unsupported jump-table relocation')
        word = struct.unpack_from(built._endian + 'I', data, relocation.offset)[0]
        destination = relocation.symbol_value + word
        if destination % 4 or not 0 <= destination < len(built.text):
            raise ValueError('jump table points outside its text range')
        struct.pack_into(built._endian + 'I', data, relocation.offset, text_start + destination)
    data.extend(bytes(row['size'] - compiled_size))
    if bytes(data) != retail_data(root, row):
        kind = 'string data' if strings else 'relocated C jump table'
        raise ValueError(f'{kind} differs from retail bytes')

    addresses = load_symbol_addresses(root / 'config/revo.symbol_addrs.txt')
    expected = {(r.offset, r.kind): r for r in retail.relocations}
    local = [r for r in built.relocations if r.symbol_section == index]
    if not local or any(r.kind not in (5, 6) or r.symbol_kind != 3 or r.addend is not None for r in local):
        raise ValueError('unsupported text reference to C rodata')
    replacements = {}
    contents = bytearray(built.text)
    for high in [r for r in local if r.kind == 5]:
        for low in [r for r in local if r.kind == 6]:
            a, b = expected.get((high.offset, 5)), expected.get((low.offset, 6))
            if not a or not b or a.symbol != b.symbol or a.symbol not in addresses:
                continue
            relative = _paired_hilo_target(built, high, low)
            expected_relative = _paired_hilo_target(retail, a, b)
            if relative is None or expected_relative is None:
                continue
            if int(row['start'], 16) + relative != addresses[a.symbol] + expected_relative:
                continue
            if not 0 <= relative < row['size']:
                continue
            for actual, reference in ((high, a), (low, b)):
                previous = replacements.get(actual.offset)
                if previous is not None and previous != reference:
                    raise ValueError('ambiguous rodata relocation normalization')
                replacements[actual.offset] = reference
    if {r.offset for r in local} != set(replacements):
        raise ValueError('C rodata reference lacks a proven matching retail address')
    for offset in replacements:
        actual = struct.unpack_from(built._endian + 'I', contents, offset)[0]
        reference = struct.unpack_from(retail._endian + 'I', retail.text, offset)[0]
        struct.pack_into(built._endian + 'I', contents, offset, (actual & 0xFFFF0000) | (reference & 0xFFFF))
    view = copy.copy(built)
    view.text = bytes(contents)
    view.relocations = [replace(r, symbol=replacements[r.offset].symbol,
                                symbol_section=replacements[r.offset].symbol_section,
                                symbol_value=replacements[r.offset].symbol_value,
                                symbol_kind=replacements[r.offset].symbol_kind)
                        if r.offset in replacements else r for r in built.relocations]
    return view
