#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools" / "buildtools"))
import posixrun

TARGET = HERE / "build" / "target"
SUBSEGMENT = re.compile(r"^(\s*- \[0x[0-9A-Fa-f]+, )(c|\.rodata|\.data)(, (\w+)\])", re.M)


def main() -> int:
    text = (HERE / "ubisound.yaml").read_text()
    units: dict[str, list[str]] = {}
    for match in SUBSEGMENT.finditer(text):
        kind, name = match.group(2), match.group(4)
        units.setdefault(name, []).append(
            f"{name}.s" if kind == "c" else f"data/{name}.{kind[1:]}.s")
    text = SUBSEGMENT.sub(lambda m: m.group(1) + {"c": "asm", ".rodata": "rodata",
                                                   ".data": "data"}[m.group(2)] + m.group(3), text)
    for key in ("asm_path", "src_path", "build_path", "ld_script_path",
                "undefined_funcs_auto_path", "undefined_syms_auto_path"):
        text = re.sub(rf"^(\s*{key}: )(\S+)", lambda m: m.group(1) + "build/target/" + m.group(2), text, flags=re.M)
    shutil.rmtree(TARGET, ignore_errors=True)
    TARGET.mkdir(parents=True)
    config = TARGET / "ubisound.yaml"
    config.write_text(re.sub(r"^(\s*base_path: )\S+", r"\g<1>../..", text, flags=re.M))
    subprocess.run([sys.executable, "-m", "splat", "split", str(config.relative_to(HERE))],
                   cwd=HERE, check=True, stdout=subprocess.DEVNULL)

    script = ["set -e", "cd ubisound", "mkdir -p build/target/obj"]
    entries = []
    for name, files in units.items():
        parts = []
        for rel in files:
            source = f"build/target/asm/{rel}"
            obj = f"build/target/obj/{rel.replace('/', '_')}.o"
            script.append(f"mipsel-linux-gnu-as -EL -march=r3000 -mabi=32 -no-pad-sections "
                          f"-Iinclude -o {obj} {source}")
            parts.append(obj)
        script.append(f"mipsel-linux-gnu-ld -r -o build/target/obj/{name}.o {' '.join(parts)}")
        entries.append({
            "name": name,
            "target_path": f"build/target/obj/{name}.o",
            "base_path": f"build/src/{name}.c.o",
            "metadata": {"complete": True, "source_path": f"src/{name}.c"},
        })
    (TARGET / "assemble.sh").write_text("\n".join(script) + "\n")
    if posixrun.run("sh ubisound/build/target/assemble.sh") != 0:
        return 1
    (HERE / "objdiff.json").write_text(json.dumps({
        "$schema": "https://raw.githubusercontent.com/encounter/objdiff/main/config.schema.json",
        "min_version": "3.0.0",
        "build_target": False,
        "build_base": False,
        "units": entries,
    }, indent=2) + "\n")
    print(f"objdiff: {len(entries)} units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
