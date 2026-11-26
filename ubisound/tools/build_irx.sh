#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
H=$PWD/tools
I=${REVO_I386_ROOT:-$PWD/../.i386-root}
X=mipsel-linux-gnu-
find asm -name '*.s' -not -path 'asm/nonmatchings/*' | sort | while read -r s; do
  mkdir -p "build/$(dirname "$s")"
  ${X}as -EL -march=r3000 -mabi=32 -no-pad-sections -Iinclude -o "build/$s.o" "$s"
done
find src -name '*.c' | sort | while read -r c; do
  mkdir -p "build/$(dirname "$c")"
  "$I/lib/ld-linux.so.2" --library-path "$I/usr/lib/i386-linux-gnu:$I/lib/i386-linux-gnu:$I/lib:$I/usr/lib" \
    "$H/iop-gcc2.8.1/bin/iop-gcc" -B"$H/shim/" -O2 -G0 -Iinclude -Wa,-Iinclude -Isrc -c "$c" -o "build/$c.o"
  ${X}strip -N dummy-symbol-name "build/$c.o"
done
${X}ld -EL -Map build/UBISOUND.IRX.map -T build/UBISOUND.IRX.ld \
  -T build/undefined_syms_auto.txt -T build/undefined_funcs_auto.txt -o build/UBISOUND.IRX.elf
${X}objcopy -O binary build/UBISOUND.IRX.elf build/UBISOUND.IRX
