#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import posixrun
from asm_includes import included_asm_files
from c_compilation_partitions import boundary_for, partition_source


def _shell_dollars(text: str) -> str:
    return text.replace("\\$", "$").replace("$", "\\$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ee_cc.py",
        description="Compile one C file with the EE compiler.",
    )
    parser.add_argument("source", help="C file, relative to the project root")
    parser.add_argument("object", help="output object, relative to the project root")
    parser.add_argument("--compiler", required=True, help="ee-gcc path")
    parser.add_argument("--includes", default="", help="preprocessor include flags")
    parser.add_argument("--flags", default="", help="compiler flags")
    parser.add_argument("--strip", default="mips-linux-gnu-strip", help="strip program")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = posixrun.posix_root()
    source = Path(args.source).as_posix()
    name = Path(source).name
    obj = Path(args.object).as_posix()
    compiler = args.compiler if args.compiler.startswith("/") else f"{root}/{args.compiler}"
    source_abs = f"{root}/{source}"
    object_abs = f"{root}/{obj}"
    object_dir = Path(obj).parent.as_posix()
    asm_inputs = included_asm_files(Path(__file__).resolve().parents[2], Path(args.source))

    script_parts = [
        "set -e",
        'tmp="$(mktemp -d /tmp/revo_ee_cc.XXXXXX)"',
        "trap 'rm -rf \"$tmp\"' EXIT",
        f"cp -r {shlex.quote(root + '/include')} \"$tmp/include\"",
        "mkdir -p \"$tmp/src\"",
        f"cp {shlex.quote(source_abs)} \"$tmp/src/{name}\"",
    ]
    for rel in asm_inputs:
        dest = Path(rel).parent.as_posix()
        script_parts.extend([
            f"mkdir -p \"$tmp/{dest}\"",
            f"cp {shlex.quote(root + '/' + rel)} \"$tmp/{rel}\"",
        ])
    source_text = Path(args.source).read_text()
    parts = partition_source(source_text, boundary_for(Path(__file__).resolve().parents[2], source))
    stripped_symbols = "-N dummy-symbol-name"
    Path(object_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ee_cc_parts_", dir=object_dir) as staging:
        inputs = [name]
        if len(parts) > 1:
            inputs = []
            for index, content in enumerate(parts):
                part = Path(staging) / f"part{index}.c"
                part.write_text(content)
                inputs.append(part.name)
                relative = part.resolve().relative_to(Path(__file__).resolve().parents[2])
                script_parts.append(
                    f"cp {shlex.quote(root + '/' + relative.as_posix())} \"$tmp/src/{part.name}\"")
        script_parts.append('cd "$tmp"')
        for item in inputs:
            script_parts.extend([
                f"{shlex.quote(compiler)} -c {_shell_dollars(args.includes)} {_shell_dollars(args.flags)} {shlex.quote('src/' + item)} -o \"$tmp/src/{item}.o\"",
                f"{args.strip} \"$tmp/src/{item}.o\" {stripped_symbols}",
            ])
        if len(parts) > 1:
            objects = " ".join(f'"$tmp/src/{item}.o"' for item in inputs)
            script_parts.append(f'mips-linux-gnu-ld -EL -r {objects} -o "$tmp/src/{name}.o"')
        if source.endswith(".cpp"):
            script_parts.append(
                f'mips-linux-gnu-objcopy -R .data -R .gcc_except_table -R .mdebug "$tmp/src/{name}.o"')
        script_parts.extend([
            f"mkdir -p {shlex.quote(root + '/' + object_dir)}",
            f"cp \"$tmp/src/{name}.o\" {shlex.quote(object_abs)}",
        ])
        return posixrun.run("; ".join(script_parts))


if __name__ == "__main__":
    sys.exit(main())
