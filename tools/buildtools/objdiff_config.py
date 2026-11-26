#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

CATEGORY_NAMES = {
    "project": "Game",
    "middleware": "Middleware",
    "external_asm": "External asm",
    "unresolved": "Unresolved",
    "renderware": "RenderWare",
    "sony_ps2_sdk": "Sony SDK",
    "gcc_newlib_runtime": "newlib / libgcc",
    "ubisoft_support": "Ubisoft support",
    "ubi": "Ubi Soft",
}

UBI_SCOPES = {"project", "ubisoft_support", "unresolved"}

GLABEL = re.compile(r"^\s*glabel\s+(\S+)\s*$")
ENDLABEL = re.compile(r"^\s*endlabel\s+(\S+)\s*$")
NEXT_GLABEL = re.compile(r"^\s*glabel\s+\S+\s*$")
NONMATCHING = re.compile(r"^\s*nonmatching\s+")


def functions_in_asm(path: Path) -> list[str]:
    found: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not inside:
            match = GLABEL.match(line)
            if match:
                found.append(match.group(1))
                inside = True
            continue
        if ENDLABEL.match(line) or NEXT_GLABEL.match(line) or NONMATCHING.match(line):
            inside = False
    return found


def categories_for(row: dict[str, str]) -> list[str]:
    categories = [row["decomp_scope"]]
    library = row["library_group"].strip()
    if library:
        categories.append(library)
    if row["decomp_scope"] in UBI_SCOPES or library == "ubisoft_support":
        categories.append("ubi")
    return categories


def make_unit(
    row: dict[str, str], base_path: str | None, target_path: str,
    asm_fallbacks: list[str] | None = None,
) -> dict:
    metadata: dict[str, object] = {
        "progress_categories": categories_for(row),
    }
    source = row["source_path"].strip()
    if source and base_path is not None:
        metadata["source_path"] = source
    if asm_fallbacks:
        metadata["asm_fallbacks"] = asm_fallbacks
    unit = {
        "name": row["splat_path"],
        "target_path": target_path,
    }
    if base_path is not None:
        unit["base_path"] = base_path
    unit["metadata"] = metadata
    return unit


def write_objdiff_json(path: Path, units: list[dict], categories: list[str]) -> None:
    config = {
        "$schema": "https://raw.githubusercontent.com/encounter/objdiff/main/config.schema.json",
        "min_version": "3.0.0",
        "custom_make": "tools/objdiff_build.cmd" if os.name == "nt" else "./tools/objdiff_build.sh",
        "build_target": True,
        "build_base": True,
        "watch_patterns": ["src/**/*.c", "include/**/*.h", "config/revo.yaml"],
        "ignore_patterns": ["build/**/*", "expected/**/*"],
        "options": {"function_reloc_diffs": "none"},
        "progress_categories": [
            {"id": category, "name": CATEGORY_NAMES.get(category, category)}
            for category in categories
        ],
        "units": units,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
