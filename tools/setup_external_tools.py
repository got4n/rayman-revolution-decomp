#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def compiler_problems(compiler: dict, root: Path) -> list[str]:
    optional = set(compiler.get("optional", []))
    problems = []
    for rel, expected in compiler["files"].items():
        path = root / rel
        if not path.is_file():
            if rel not in optional:
                problems.append(rel)
        elif digest(path.read_bytes()) != expected:
            problems.append(rel)
    return problems


def download_compiler(compiler: dict) -> None:
    staged = {}
    for entry in compiler["archives"]:
        name = entry["url"].rsplit("/", 1)[-1]
        data = fetch(entry["url"])
        if digest(data) != entry["sha256"]:
            raise SystemExit(f"download hash mismatch: {name}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for rel, member_name in entry["members"].items():
                member = archive.getmember(member_name)
                if not member.isfile():
                    raise SystemExit(f"unexpected archive member: {name}: {member_name}")
                with archive.extractfile(member) as handle:
                    content = handle.read()
                if digest(content) != compiler["files"][rel]:
                    raise SystemExit(f"compiler file hash mismatch: {rel} from {name}")
                staged[rel] = content
    for rel, content in staged.items():
        dest = ROOT / compiler["destination"] / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler-root", type=Path,
                        help="use your own EE compiler tree with bin/ and lib/ instead of downloading it")
    parser.add_argument("--download", action="store_true",
                        help="download the pinned EE compiler, Slinky and Objdiff releases")
    args = parser.parse_args()
    lock = json.loads((ROOT / "config/external_tools.json").read_text())
    compiler = lock["compiler"]
    if args.compiler_root:
        source = args.compiler_root.resolve()
        missing = compiler_problems(compiler, source)
        if missing:
            raise SystemExit(f"compiler input missing or hash mismatch: {', '.join(missing)}")
        for rel in compiler["files"]:
            if not (source / rel).is_file():
                continue
            dest = ROOT / compiler["destination"] / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (source / rel).resolve() != dest.resolve():
                shutil.copy2(source / rel, dest)
            dest.chmod(dest.stat().st_mode | 0o111)
    elif args.download and compiler_problems(compiler, ROOT / compiler["destination"]):
        download_compiler(compiler)
    if args.download:
        for name, entry in lock["downloads"].items():
            dest = ROOT / entry["destination"]
            if dest.is_file() and digest(dest.read_bytes()) == entry["binary_sha256"]:
                continue
            with urllib.request.urlopen(entry["url"], timeout=60) as response:
                data = response.read()
            if digest(data) != entry["sha256"]:
                raise SystemExit(f"download hash mismatch: {name}")
            if entry["tar_member"]:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    member = archive.getmember(entry["tar_member"])
                    if not member.isfile():
                        raise SystemExit(f"unexpected archive member: {name}")
                    with archive.extractfile(member) as handle:
                        data = handle.read()
            if digest(data) != entry["binary_sha256"]:
                raise SystemExit(f"executable hash mismatch: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o755)
    problems = [f"compiler: {rel}" for rel in compiler_problems(compiler, ROOT / compiler["destination"])]
    for name, entry in lock["downloads"].items():
        path = ROOT / entry["destination"]
        if not path.is_file() or digest(path.read_bytes()) != entry["binary_sha256"]:
            problems.append(name)
    if problems:
        for problem in problems:
            print(f"missing or hash mismatch: {problem}")
        return 1
    print("OK: pinned compiler, Slinky 0.3.0 and Objdiff 3.6.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
