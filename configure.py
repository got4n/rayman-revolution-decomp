#!/usr/bin/env python3
import argparse
import hashlib
import importlib.util
from functools import lru_cache
import os
import re
import shutil
import struct
import subprocess
import sys
import json

from pathlib import Path
from typing import Dict, List, Set, Union

_PYTHON_MODULES = ("ninja_syntax", "spimdisasm", "splat")


def _missing_python_modules() -> list[str]:
    return [name for name in _PYTHON_MODULES if importlib.util.find_spec(name) is None]


if "--check" in sys.argv:
    _missing_modules = _missing_python_modules()
    if _missing_modules:
        print("[ENVIRONMENT] missing python modules:", file=sys.stderr)
        for _name in _missing_modules:
            print(f"  - {_name}", file=sys.stderr)
        print("python3 -m pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(1)

import ninja_syntax
import spimdisasm
import splat
import splat.scripts.split as split
from splat.segtypes.linker_entry import LinkerEntry

ROOT = Path(__file__).resolve().parent
TOOLS_DIR = ROOT / "tools"
BUILDTOOLS_DIR = TOOLS_DIR / "buildtools"

sys.path.insert(0, str(BUILDTOOLS_DIR))
import objdiff_config
from asm_includes import included_asm_files
from c_rodata import retained_layout
from c_literals import retained_layout as literal_layout

REVO_YAML_FILE = "config/revo.yaml"
REVO_BASENAME  = "SLUS_201.38"
REVO_LD_PATH   = f"{REVO_BASENAME}.ld"
REVO_ROM_PATH  = f"build/{REVO_BASENAME}.rom"
REVO_MAP_PATH  = f"build/{REVO_BASENAME}.map"
REVO_ELF_PATH  = f"build/{REVO_BASENAME}.elf"
REVO_SHA1_PATH = "config/revo.checksum.sha1"
REVO_SLINKY_PATH = "config/revo.slinky.yaml"

OBJDIFF_JSON = "objdiff.json"
OBJDIFF_EXPECTED = "expected/build/asm"
OBJDIFF_BUILD_SRC = "build/src"
OBJDIFF_NORMALIZED = "build/objdiff-base"

def _python() -> str:
    return sys.executable or shutil.which("python3") or shutil.which("python") or "python3"


def _quote(token: str) -> str:
    if any(char in token for char in " \t"):
        return f'"{token}"'
    return token


def _ninja() -> List[str]:
    exe = shutil.which("ninja")
    if exe:
        return [exe]
    if importlib.util.find_spec("ninja") is not None:
        return [sys.executable, "-m", "ninja"]
    return []


PY = _quote(_python())
POSIXRUN = f'{PY} tools/buildtools/posixrun.py'
CHECK_SHA1 = f'{PY} tools/buildtools/check_sha1.py'


def _posix(script: str) -> str:
    if '"' in script:
        raise SystemExit(f"internal error: quote in build command: {script}")
    return f'{POSIXRUN} "{script}"'


EM_386 = 3


def _is_i386_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return (
        len(header) == 20
        and header[:4] == b"\x7fELF"
        and header[4] == 1
        and struct.unpack_from("<H", header, 18)[0] == EM_386
    )


def _ee_gcc() -> str:
    override = os.environ.get("REVO_EE_GCC")
    if override:
        return override

    native_rel = "tools/toolchain/ee-gcc29/bin/ee-gcc"
    wrapper_rel = "tools/buildtools/ee-gcc-qemu.sh"
    if not (ROOT / wrapper_rel).is_file():
        wrapper_rel = "i386-deps/compile-c-qemu.sh"
    native = ROOT / native_rel
    wrapper = ROOT / wrapper_rel

    if native.is_file():
        if _is_i386_elf(native) and sys.platform != "win32" and wrapper.is_file():
            return wrapper_rel
        return native_rel
    if wrapper.is_file():
        return wrapper_rel
    return native_rel


EE_GCC = _ee_gcc()

EE_COMMON_INCLUDES = "-Iinclude -Isrc"

EE_COMPILER_FLAGS = "-O2 -G8 -fno-strict-aliasing -g -Wa,-Iinclude"

EE_SOURCE_FLAGS = {
    "src/tempgrp/MEC/MECPfm.c": "-O2 -G8 -fno-strict-aliasing -g -Wa,-Iinclude",
    "src/tempgrp/MEC/MECTools_part2.c": "-O2 -G8 -fno-strict-aliasing -g -Wa,-Iinclude",
}

CROSS = "mips-linux-gnu-"

REQUIRED_POSIX_TOOLS = ("cpp", f"{CROSS}as", f"{CROSS}strip", f"{CROSS}ld", f"{CROSS}objcopy")


def exec_shell(command: List[str], stdout=subprocess.PIPE) -> str:
    ret = subprocess.run(command, stdout=stdout, stderr=subprocess.PIPE, text=True)
    if ret.returncode != 0:
        if ret.stderr:
            sys.stderr.write(ret.stderr)
    return ret.stdout or ""


def clean():
    for f in [".splache", ".ninja_log", "build.ninja", REVO_LD_PATH]:
        if os.path.exists(f):
            os.remove(f)
    shutil.rmtree("asm", ignore_errors=True)
    shutil.rmtree("build", ignore_errors=True)
    shutil.rmtree("expected", ignore_errors=True)


def snapshot_asm_files() -> Dict[Path, tuple[bytes, int]]:
    asm_root = ROOT / "asm"
    if not asm_root.is_dir():
        return {}
    snapshot = {}
    for path in asm_root.rglob("*.s"):
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            snapshot[path] = (hashlib.sha256(path.read_bytes()).digest(), stat.st_mtime_ns)
    return snapshot


def restore_unchanged_asm_mtimes(snapshot: Dict[Path, tuple[bytes, int]]) -> None:
    for path, (digest, mtime_ns) in snapshot.items():
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        if stat.st_mtime_ns != mtime_ns and hashlib.sha256(path.read_bytes()).digest() == digest:
            try:
                os.utime(path, ns=(stat.st_atime_ns, mtime_ns))
            except OSError:
                pass


def check_environment() -> List[str]:
    missing: List[str] = []

    if not _ninja():
        missing.append("ninja (host)")

    probes = [
        f"command -v {tool} >/dev/null || echo 'missing:{tool}'"
        for tool in REQUIRED_POSIX_TOOLS
    ]
    probes.append(f"test -x {EE_GCC} || echo 'missing:{EE_GCC}'")
    probes.append("test -x tools/slinky/slinky-cli || echo 'missing:tools/slinky/slinky-cli'")

    posixrun = str(BUILDTOOLS_DIR / "posixrun.py")
    done = subprocess.run(
        [_python(), posixrun, "; ".join(probes)],
        capture_output=True,
        text=True,
    )
    for line in (done.stdout or "").splitlines():
        if line.startswith("missing:"):
            missing.append(line.split(":", 1)[1])
    if done.returncode != 0 and not missing:
        detail = ((done.stdout or "") + (done.stderr or "")).strip()
        if detail:
            print(detail, file=sys.stderr)
        missing.append("the POSIX build environment")

    return missing


def environment_hint() -> str:
    if sys.platform == "win32":
        return (
            "The Linux toolchain runs inside WSL on Windows. In an Ubuntu distribution:\n"
            "  sudo apt update\n"
            "  sudo apt install python3 ninja-build binutils-mips-linux-gnu gcc-mipsel-linux-gnu cpp\n"
            "  sudo dpkg --add-architecture i386 && sudo apt install libc6:i386"
        )
    if sys.platform == "darwin":
        return (
            "The bundled compiler is a Linux i386 binary. On this Mac the tested\n"
            "path is Docker (tools/docker/Dockerfile) plus .i386-root. A Rosetta\n"
            "container is not the default. Linux and Windows are separate checks."
        )
    return (
        "  sudo apt update\n"
        "  sudo apt install python3 ninja-build binutils-mips-linux-gnu gcc-mipsel-linux-gnu cpp\n"
        "  sudo dpkg --add-architecture i386 && sudo apt install libc6:i386"
    )


def write_permuter_settings():
    with open("permuter_settings.toml", "w") as f:
        f.write(
            f'compiler_command = "{EE_GCC} -c {EE_COMMON_INCLUDES} -O2 -G8 -fno-strict-aliasing -gstabs -D__GNUC__"\n'
            f'assembler_command = "mips-linux-gnu-as -march=r5900 -mabi=eabi -Iinclude"\n'
            f'compiler_type = "gcc"\n'
            f'\n'
            f'[preserve_macros]\n'
            f'\n'
            f'[decompme.compilers]\n'
            f'"{EE_GCC}" = "ee-gcc2.9-991111-01"\n'
        )


VRAM_TO_FILE = 0xFF000
C_SEGMENT = re.compile(r"^\s+- \[0x([0-9A-Fa-f]+), (c|cpp), ([\w./+-]+)\]")
TEXT_ENTRY = re.compile(r"^\s+- \[0x([0-9A-Fa-f]+)(?:, (\w+)(?:, ([\w./+-]+))?)?")
LIBRARY_SCOPES = {
    "sdk": ("external_asm", "sony_ps2_sdk"),
    "gcc": ("external_asm", "gcc_newlib_runtime"),
    "rwsdk": ("middleware", "renderware"),
}


def load_tu_splits() -> List[dict]:
    entries = []
    for line in (ROOT / REVO_YAML_FILE).read_text(encoding="utf-8").splitlines():
        match = TEXT_ENTRY.match(line)
        if match:
            entries.append((int(match.group(1), 16), match.group(2), match.group(3)))
    rows = []
    for index, (offset, kind, name) in enumerate(entries):
        if kind not in ("asm", "c", "cpp"):
            continue
        if index + 1 == len(entries):
            raise SystemExit(f"{REVO_YAML_FILE}: {name} has no end")
        start, end = offset + VRAM_TO_FILE, entries[index + 1][0] + VRAM_TO_FILE
        if kind == "asm":
            scope = LIBRARY_SCOPES.get(name.split("/")[0])
            if scope is None:
                raise SystemExit(f"{REVO_YAML_FILE}: unknown library for {name}")
            source, tu = "", name
        else:
            scope = ("project", "ubisoft_support" if "/ubi/" in name else "")
            source, tu = f"src/{name}.{kind}", "main/" + name.rsplit("/", 1)[-1]
        rows.append({
            "start": f"0x{start:08X}",
            "end": f"0x{end:08X}",
            "size": str(end - start),
            "source_path": source,
            "library_group": scope[1],
            "decomp_scope": scope[0],
            "splat_path": tu,
        })
    if not rows:
        raise SystemExit(f"{REVO_YAML_FILE} lists no text splits")
    return rows


def linked_c_objects() -> Dict[int, str]:
    found: Dict[int, str] = {}
    text = (ROOT / REVO_YAML_FILE).read_text(encoding="utf-8")
    for line in text.splitlines():
        match = C_SEGMENT.match(line)
        if match:
            found[int(match.group(1), 16)] = f"src/{match.group(3)}.{match.group(2)}.o"
    return found


def _asm_segment_syntax(seg) -> None:
    from rabbitizer import TrinaryValue

    section = seg.spim_section.get_section()
    section.setGpRelHack(False)
    for func in section.symbolList:
        for insn in func.instructions:
            insn.flag_r5900UseDollar = TrinaryValue.TRUE


def emit_retail_asm_for_linked_c(linker_entries: List[LinkerEntry], splits: List[dict]) -> None:
    linked = linked_c_objects()
    by_offset = {}
    for entry in linker_entries:
        seg = entry.segment
        if getattr(seg, "type", None) not in ("c", "cpp") or seg.rom_start is None:
            continue
        by_offset[int(seg.rom_start)] = seg
    for row in splits:
        offset = int(row["start"], 16) - VRAM_TO_FILE
        if offset not in linked:
            continue
        seg = by_offset.get(offset)
        if seg is None:
            raise SystemExit(f"objdiff asm: no C segment at file offset {offset:#x}")
        if seg.spim_section is None:
            seg.scan_code((ROOT / "iso" / REVO_BASENAME).read_bytes())
        _asm_segment_syntax(seg)
        dest = ROOT / "asm" / f"{row['splat_path']}.s"
        seg.split_as_asm_file(dest)
        if not dest.is_file():
            raise SystemExit(f"objdiff asm: {dest} was not written")


def write_slinky_config(splits: List[dict]) -> None:
    lines = [
        "settings:",
        "  base_path: build",
        "",
        "  sections_allowlist_extra:",
        "    - .shstrtab",
        "    - .symtab",
        "    - .strtab",
        "",
        "  discard_wildcard_section: False",
        "  sections_denylist:",
        "    - .reginfo",
        "    - .MIPS.abiflags",
        "    - .gnu.attributes",
        "    - .pdr",
        "",
        "segments:",
        "  - name: text",
        "    fixed_vram: 0x00100000",
        "    alloc_sections: [.text]",
        "    files:",
    ]
    linked = linked_c_objects()
    for row in splits:
        offset = int(row["start"], 16) - VRAM_TO_FILE
        if offset in linked:
            lines.append(f"      - {{ path: {linked[offset]} }}")
        else:
            lines.append(f"      - {{ path: asm/{row['splat_path']}.s.o }}")
    rodata_paths, _ = retained_layout(ROOT, splits, linked)
    sdata_paths, _ = retained_layout(ROOT, splits, linked, '.sdata')
    literal_paths, _ = literal_layout(ROOT, splits, linked)
    lines += [
        "  - name: vutext",
        "    fixed_vram: 0x0022EAA0",
        "    alloc_sections: [.data]",
        "    files:",
        "      - { path: asm/data/main/vutext.s.o }",
        "  - name: data",
        "    fixed_vram: 0x00252380",
        "    alloc_sections: [.data]",
        "    files:",
        "      - { path: asm/data/main/data.data.s.o }",
        "      - { path: asm/data/main/vudata.data.s.o }",
        "  - name: rodata",
        "    fixed_vram: 0x00388D80",
        "    alloc_sections: [.rodata]",
        *(["    subalign: 4"] if rodata_paths != ['asm/data/main/rodata.rodata.s.o'] else []),
        "    files:",
        *[f"      - {{ path: {path} }}" for path in rodata_paths],
        "      - { path: asm/data/main/gcc_except_table.rodata.s.o }",
        *(["      - { path: asm/data/main/lit4.rodata.s.o }"] if not literal_paths else [
            "  - name: literals", "    fixed_vram: 0x00392700",
            "    alloc_sections: [.lit4]", "    subalign: 4", "    files:",
            *[f"      - {{ path: {path} }}" for path in literal_paths],
        ]),
        "  - name: sdata",
        "    fixed_vram: 0x00393200",
        "    alloc_sections: [.data, .sdata]",
        *(["    subalign: 4"] if sdata_paths != ['asm/data/main/sdata.data.s.o'] else []),
        "    files:",
        *[f"      - {{ path: {path} }}" for path in sdata_paths],
        "  - name: sbss",
        "    fixed_vram: 0x00393F00",
        "    alloc_sections: [.sbss]",
        "    files:",
        "      - { path: asm/data/main/sbss.sbss.s.o }",
        "  - name: bss",
        "    fixed_vram: 0x00394100",
        "    alloc_sections: [.bss]",
        "    files:",
        "      - { path: asm/data/main/bss.bss.s.o }",
        "",
        "entry: start",
    ]
    output = ROOT / REVO_SLINKY_PATH
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if not output.is_file() or output.read_bytes() != content:
        output.write_bytes(content)


INCLUDE_DIRECTIVE = re.compile(r'^\s*(?:#\s*include|\.include)\s*[<"]([^">]+)[">]', re.M)


@lru_cache(maxsize=None)
def direct_repo_includes(source: str) -> tuple[str, ...]:
    path = ROOT / source
    if not path.is_file():
        return ()
    found = []
    for name in INCLUDE_DIRECTIVE.findall(path.read_text(encoding="utf-8", errors="replace")):
        for candidate in (path.parent / name, ROOT / "include" / name, ROOT / "src" / name, ROOT / name):
            if candidate.is_file():
                try:
                    found.append(candidate.resolve().relative_to(ROOT).as_posix())
                except ValueError:
                    pass
                break
    return tuple(found)


def compilation_headers(source: str) -> List[str]:
    dependencies: Set[str] = set()
    visited: Set[str] = set()
    pending = [source]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for included in direct_repo_includes(current):
            dependencies.add(included)
            pending.append(included)
    if "include/include_asm.h" in dependencies:
        dependencies.update(("include/macro.inc", "include/labels.inc"))
    if source.endswith((".c", ".cpp")):
        dependencies.update(("tools/buildtools/ee_cc.py",
                             "tools/buildtools/c_compilation_partitions.py",
                             "config/revo.c_compilation_partitions.json"))
    return sorted(dependencies)


def build_stuff(linker_entries: List[LinkerEntry]):
    built_objects: Set[Path] = set()
    ninja = ninja_syntax.Writer(open(str(ROOT / "build.ninja"), "w"), width=9999)
    slinky_args = "--omit-version-comment"
    common_ld_args = f"-EL -G 0 --defsym _gp=0x39A6F0 -Map $mapfile -T $in -o $out -T config/revo.undefined_syms.txt"

    ninja.rule(
        "ee_as",
        description="ee_as $in",
        command=(
            _posix(f"cpp {EE_COMMON_INCLUDES} $in -o - | {CROSS}as -no-pad-sections -EL -march=5900 -mabi=eabi -Iinclude -o $out")
            + " --then tools/buildtools/elf_patcher.py $out gas"
        ),
    )

    ninja.rule(
        "ee_cc",
        description="ee_cc $in",
        command=(
            f"{PY} tools/buildtools/ee_cc.py $in $out"
            f" --compiler {EE_GCC}"
            f' --includes "{EE_COMMON_INCLUDES}"'
            ' --flags "$flags"'
        ),
    )

    ninja.rule(
        "slink",
        description="slink $out",
        command=_posix(f"tools/slinky/slinky-cli -o $out {slinky_args} $in"),
    )

    ninja.rule(
        "ee_ld",
        description="ee_ld $out",
        command=_posix(f"{CROSS}ld {common_ld_args}"),
    )

    ninja.rule(
        "rom",
        description="rom $out",
        command=_posix(f"{CROSS}objcopy $in $out -O binary"),
    )

    object_paths: Set[str] = set()

    for entry in linker_entries:
        seg = entry.segment

        if seg.type[0] == ".":
            continue
        if entry.object_path is None:
            continue
        object_path = Path(entry.object_path).as_posix()
        if object_path in object_paths:
            print(f"Removing duplicate entry for: {entry.object_path}")
            continue
        object_strs = [object_path]
        src_strs = [Path(s).as_posix() for s in entry.src_paths]

        if isinstance(seg, splat.segtypes.common.asm.CommonSegAsm) or isinstance(
            seg, splat.segtypes.common.data.CommonSegData
        ):
            object_paths.add(object_path)
            ninja.build(object_strs, "ee_as", src_strs, implicit=compilation_headers(src_strs[0]))
        elif isinstance(seg, splat.segtypes.common.c.CommonSegC):
            object_paths.add(object_path)
            asm_inputs = included_asm_files(ROOT, ROOT / src_strs[0])
            ninja.build(object_strs, "ee_cc", src_strs,
                        implicit=[*compilation_headers(src_strs[0]), *asm_inputs],
                        variables={"flags": _ninja_escape(EE_SOURCE_FLAGS.get(src_strs[0], EE_COMPILER_FLAGS))})
        elif isinstance(seg, splat.segtypes.common.databin.CommonSegDatabin) or isinstance(
            seg, splat.segtypes.common.rodatabin.CommonSegRodatabin
        ):
            object_paths.add(object_path)
            ninja.build(object_strs, "ee_as", src_strs, implicit=compilation_headers(src_strs[0]))
        elif seg.type == "bin":
            pass
        else:
            print(f"ERROR: Unsupported build segment type {seg.type}")
            sys.exit(1)
    _, rodata_sources = retained_layout(ROOT, load_tu_splits(), linked_c_objects())
    _, sdata_sources = retained_layout(ROOT, load_tu_splits(), linked_c_objects(), '.sdata')
    _, literal_sources = literal_layout(ROOT, load_tu_splits(), linked_c_objects())
    for source in [*rodata_sources, *sdata_sources, *literal_sources]:
        output = "build/" + source + ".o"
        ninja.build(output, "ee_as", source, implicit=["iso/" + REVO_BASENAME])
        object_paths.add(output)
    ninja.build(REVO_LD_PATH, "slink", "config/revo.slinky.yaml")

    ninja.build(
        REVO_ELF_PATH,
        "ee_ld",
        REVO_LD_PATH,
        implicit=[obj for obj in object_paths],
        variables={"mapfile": REVO_MAP_PATH},
    )

    ninja.build(REVO_ROM_PATH, "rom", REVO_ELF_PATH)

    ninja.rule(
        "sha1sum",
        description="sha1sum $out",
        command=f"{CHECK_SHA1} $checksum $rom $out",
    )
    ninja.build(
        REVO_ROM_PATH + ".ok",
        "sha1sum",
        [
            REVO_SHA1_PATH,
            REVO_ROM_PATH,
            "iso/SLUS_201.38",
            "tools/buildtools/check_sha1.py",
        ],
        variables={"checksum": REVO_SHA1_PATH, "rom": REVO_ROM_PATH},
    )

    return ninja


def _ninja_escape(value: str) -> str:
    return value.replace("\\$", "$").replace("$", "\\$$")


def build_objdiff(ninja) -> None:
    splits = load_tu_splits()

    ninja.rule(
        "copy_file",
        description="copy $out",
        command=f"{PY} tools/buildtools/copy_file.py $in $out",
    )
    ninja.rule(
        "ee_cc_flags",
        description="ee_cc $in",
        command=(
            f"{PY} tools/buildtools/ee_cc.py $in $out"
            f" --compiler {EE_GCC}"
            f' --includes "{EE_COMMON_INCLUDES}"'
            f' --flags "$flags"'
        ),
    )
    ninja.rule(
        "objdiff_normalize",
        description="objdiff base $out",
        command=f"{PY} tools/buildtools/objdiff_normalize.py $target $in $linked $out",
    )

    expected_objects: List[str] = []
    base_objects: List[str] = []
    compile_rules: Dict[str, tuple[str, str]] = {}
    units: List[dict] = []

    linked = linked_c_objects()
    for row in splits:
        tu = row["splat_path"]
        asm_file = ROOT / "asm" / f"{tu}.s"
        if not asm_file.is_file():
            raise SystemExit(f"missing retail asm: {asm_file}")

        expected = f"{OBJDIFF_EXPECTED}/{tu}.o"
        offset = int(row["start"], 16) - VRAM_TO_FILE
        if offset in linked:
            retail_obj = f"build/objdiff-asm/{tu}.s.o"
            ninja.build([retail_obj], "ee_as", [f"asm/{tu}.s"], implicit=compilation_headers(f"asm/{tu}.s"))
        else:
            retail_obj = f"build/asm/{tu}.s.o"
        ninja.build([expected], "copy_file", [retail_obj])
        expected_objects.append(expected)

        base = f"{OBJDIFF_BUILD_SRC}/{tu}.o"
        merged = row["source_path"].strip() if offset in linked else None
        if merged is not None:
            compile_rules[base] = (merged, EE_SOURCE_FLAGS.get(merged, EE_COMPILER_FLAGS))
            normalized = f"{OBJDIFF_NORMALIZED}/{tu}.o"
            linked_obj = f"build/{linked[offset]}"
            ninja.build(
                [normalized],
                "objdiff_normalize",
                [base],
                implicit=[
                    expected, linked_obj, REVO_ROM_PATH + ".ok",
                    "tools/buildtools/objdiff_normalize.py", "tools/buildtools/elf_match.py",
                ],
                variables={"target": expected, "linked": linked_obj},
            )
            base = normalized
            base_objects.append(base)
        else:
            base = None
        included = included_asm_files(ROOT, ROOT / merged) if merged is not None else []
        fallbacks = [Path(path).stem for path in included]
        unknown_fallbacks = set(fallbacks) - set(objdiff_config.functions_in_asm(asm_file))
        if unknown_fallbacks:
            raise SystemExit(f"{tu}: INCLUDE_ASM outside retail split: {sorted(unknown_fallbacks)}")
        units.append(objdiff_config.make_unit(row, base, expected, fallbacks))

    for obj, (source, flags) in compile_rules.items():
        ninja.build(
            [obj],
            "ee_cc_flags",
            [source],
            implicit=[*compilation_headers(source), *included_asm_files(ROOT, ROOT / source)],
            variables={"flags": _ninja_escape(flags)},
        )

    ninja.build("objdiff-targets", "phony", expected_objects)
    ninja.build("objdiff-bases", "phony", base_objects)
    ninja.build("objdiff", "phony", ["objdiff-targets", "objdiff-bases"])

    categories = sorted({
        category
        for row in splits
        for category in objdiff_config.categories_for(row)
    })
    objdiff_config.write_objdiff_json(ROOT / OBJDIFF_JSON, units, categories)
    print(
        f"objdiff: {len(units)} units, {len(expected_objects)} target objects, "
        f"{len(compile_rules)} C objects, {len(base_objects)} base links"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure the project")
    parser.add_argument(
        "-noc", "--noclean",
        help="Skip cleaning of split and build artifacts",
        action="store_true",
    )
    parser.add_argument(
        "-csrc", "--cleansrc",
        help="Clean the 'src' folder",
        action="store_true",
    )
    parser.add_argument(
        "--check",
        help="Only check the build environment, then exit",
        action="store_true",
    )
    parser.add_argument(
        "--no-env-check",
        help="Skip the build environment check",
        action="store_true",
    )
    args = parser.parse_args()

    if args.check or not args.no_env_check:
        missing = check_environment()
        if missing:
            print("[ENVIRONMENT] missing from the build environment:")
            for item in missing:
                print(f"  - {item}")
            print(environment_hint())
            sys.exit(1)
        if args.check:
            print(f"OK environment (python: {_python()}, compiler: {EE_GCC})")
            sys.exit(0)

    if not args.noclean:
        clean()

    if args.cleansrc:
        shutil.rmtree("src", ignore_errors=True)

    asm_snapshot = snapshot_asm_files() if args.noclean else {}
    split.main([Path(REVO_YAML_FILE)], modes=["all"], verbose=False)
    linker_entries = split.linker_writer.entries
    splits = load_tu_splits()
    emit_retail_asm_for_linked_c(linker_entries, splits)
    restore_unchanged_asm_mtimes(asm_snapshot)
    write_slinky_config(splits)
    ninja = build_stuff(linker_entries)
    build_objdiff(ninja)
    ninja.default(REVO_ROM_PATH + ".ok")
    ninja.close()

    write_permuter_settings()

    ninja_cmd = _ninja()
    if ninja_cmd:
        exec_shell([*ninja_cmd, "-t", "compdb"], open("compile_commands.json", "w"))
    else:
        print("warning: ninja was not found, compile_commands.json was not regenerated")
