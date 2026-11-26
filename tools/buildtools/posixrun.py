#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _wsl_exe() -> str:
    return shutil.which("wsl") or shutil.which("wsl.exe") or "wsl.exe"


def _windows_to_wsl(path: Path) -> str | None:
    try:
        done = subprocess.run(
            [_wsl_exe(), "wslpath", "-a", path.as_posix()],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    guest = done.stdout.strip()
    return guest if done.returncode == 0 and guest else None


_POSIX_ROOT: str | None = None


def posix_root() -> str:
    global _POSIX_ROOT
    if _POSIX_ROOT is None:
        _POSIX_ROOT = _resolve_posix_root()
    return _POSIX_ROOT


def i386_root() -> Path | None:
    override = os.environ.get("REVO_I386_ROOT")
    if override:
        path = Path(override)
        qemu = path / "usr/bin/qemu-i386-static"
        return path if qemu.is_file() else None
    local = ROOT / ".i386-root"
    if (local / "usr/bin/qemu-i386-static").is_file():
        return local
    return None


def use_docker() -> bool:
    backend = os.environ.get("REVO_POSIX_BACKEND", "")
    if backend == "local" or backend == "container" or sys.platform == "win32":
        return False
    if backend == "docker":
        return shutil.which("docker") is not None and i386_root() is not None
    return (
        sys.platform == "darwin"
        and shutil.which("docker") is not None
        and i386_root() is not None
    )


def _resolve_posix_root() -> str:
    if sys.platform == "win32":
        guest = _windows_to_wsl(ROOT)
        if guest is None:
            raise SystemExit(
                "windows build: WSL is required to run the Linux toolchain.\n"
                "Install a distribution (wsl --install -d Ubuntu), install the\n"
                "packages listed in docs/build.md inside it, then run this again."
            )
        return guest
    if use_docker():
        return "/work"
    if sys.platform == "darwin" and os.environ.get("REVO_EE_IMAGE") and shutil.which("container"):
        return "/revo"
    return str(ROOT)


def _running_container() -> str | None:
    name = os.environ.get("REVO_DOCKER_NAME", "revo-ee-build")
    if not name or shutil.which("docker") is None:
        return None
    done = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
    )
    if done.returncode == 0:
        try:
            info = json.loads(done.stdout)[0]
            mounts = {m["Destination"]: Path(m["Source"]).resolve()
                      for m in info.get("Mounts", []) if m.get("Type") == "bind"}
            expected_runtime = i386_root()
            expected_image = os.environ.get("REVO_DOCKER_IMAGE", "revo-ee:bookworm")
            if (info.get("State", {}).get("Running")
                    and mounts.get("/work") == ROOT.resolve()
                    and expected_runtime is not None
                    and mounts.get("/i386") == expected_runtime.resolve()
                    and info.get("Config", {}).get("Image") == expected_image):
                return name
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    return None


def _docker_wrap(command: str) -> list[str]:
    guest = f"cd /work && {command}"
    running = _running_container()
    if running:
        return ["docker", "exec", "-e", "REVO_I386_ROOT=/i386", "-w", "/work", running, "bash", "-c", guest]
    rootfs = i386_root()
    image = os.environ.get("REVO_DOCKER_IMAGE", "revo-ee:bookworm")
    if rootfs is None:
        raise SystemExit("docker backend: .i386-root or REVO_I386_ROOT is missing")
    return [
        "docker", "run", "--rm",
        "--platform", "linux/amd64",
        "-v", f"{ROOT}:/work",
        "-v", f"{rootfs}:/i386:ro",
        "-w", "/work",
        "-e", "REVO_I386_ROOT=/i386",
        image,
        "bash", "-c", guest,
    ]


def wrap(command: str) -> list[str]:
    if sys.platform == "win32":
        argv = [_wsl_exe()]
        distro = os.environ.get("REVO_WSL_DISTRO")
        if distro:
            argv += ["--distribution", distro]
        shell = shlex.split(os.environ.get("REVO_WSL_SHELL", "bash -c"))
        return argv + ["-e", *shell, f"cd {_shell_quote(posix_root())} && {command}"]

    if use_docker():
        return _docker_wrap(command)

    image = os.environ.get("REVO_EE_IMAGE")
    if sys.platform == "darwin" and image and shutil.which("container"):
        guest = command.replace(str(ROOT), posix_root())
        return [
            "container", "run", "--rm",
            "-v", f"{ROOT}:/revo",
            "-w", "/revo",
            image,
            "bash", "-c", guest,
        ]

    if sys.platform == "darwin":
        raise SystemExit(
            "macOS: no i386 backend is selected. Build tools/docker/Dockerfile "
            "and place qemu-i386 in .i386-root, or set REVO_EE_IMAGE. "
            "A Rosetta container is not the default."
        )

    shell = shlex.split(os.environ.get("REVO_POSIX_SHELL", "bash -c"))
    return [*shell, f"cd {_shell_quote(posix_root())} && {command}"]


def run(command: str) -> int:
    try:
        return subprocess.run(wrap(command)).returncode
    except FileNotFoundError as exc:
        print(f"posixrun: cannot execute {exc.filename!r}", file=sys.stderr)
        return 127


def run_host_script(argv: list[str]) -> int:
    script = Path(argv[0])
    if not script.is_absolute():
        script = ROOT / script
    return subprocess.run([sys.executable, str(script), *argv[1:]], cwd=ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="posixrun.py",
        description="Run one command in the POSIX build environment.",
    )
    parser.add_argument("command", help="command to run (quoted as one argument)")
    parser.add_argument(
        "--then",
        nargs="+",
        metavar="ARG",
        help="script to run on this host after the command succeeds",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    code = run(args.command)
    if code != 0 or not args.then:
        return code
    return run_host_script(args.then)


if __name__ == "__main__":
    sys.exit(main())
