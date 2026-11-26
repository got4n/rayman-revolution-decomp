# Build

Put your own copies of the retail files in `iso/`:

| File | SHA-1 |
|------|-------|
| `iso/SLUS_201.38` | `33618569b7a1aa23300e9d1139510220189b3abe` |
| `iso/UBISOUND.IRX` (disc `MODS/`) | `2de98daa619434abe80d95a4fe8fefd1a49a8b09` |

The compilers are Linux i386 programs: use WSL on Windows and Docker on macOS.

```bash
sudo apt install python3 python3-pip ninja-build binutils-mips-linux-gnu gcc-mipsel-linux-gnu cpp
sudo dpkg --add-architecture i386 && sudo apt install libc6:i386 libstdc++6:i386
python3 -m pip install -r requirements.txt
python3 tools/setup_external_tools.py --download
python3 configure.py
ninja
```

`setup_external_tools.py --download` fetches the pinned EE GCC 2.9, Slinky and objdiff from their releases and checks them against `config/external_tools.json`.

On Windows, install the apt packages inside WSL and run the Python and ninja commands from PowerShell or cmd. On macOS, build the image once instead of the apt step:

```bash
python3 tools/setup_i386_runtime.py
docker build --platform linux/amd64 -t revo-ee:bookworm -f tools/docker/Dockerfile tools/docker
```

`python3 configure.py --check` lists anything missing. The build should end with `build/SLUS_201.38.rom.ok: OK`.

For `UBISOUND.IRX`, run `python3 ubisound/build.py`. It downloads the pinned `iop-gcc2.8.1` and prints `UBISOUND.IRX: OK` when the module matches.

For objdiff, run `ninja objdiff` and `python3 ubisound/tools/objdiff_units.py`, then open `objdiff.json` or `ubisound/objdiff.json`.
