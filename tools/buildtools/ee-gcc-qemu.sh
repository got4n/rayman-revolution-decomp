#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
GCC_VERSION="2.9-ee-991111-01"
COMPILER_ROOT="${ROOT}/tools/toolchain/ee-gcc29"
EE="${COMPILER_ROOT}/bin/ee-gcc"
CC1="${COMPILER_ROOT}/lib/gcc-lib/ee/${GCC_VERSION}/cc1"
AS="${COMPILER_ROOT}/ee/bin/as"
I386="${REVO_I386_ROOT:-${ROOT}/.i386-root}"
QEMU="${I386}/usr/bin/qemu-i386-static"

if [[ ! -f "${CC1}" || ! -f "${AS}" ]]; then
  echo "ee-gcc-qemu: missing the EE compiler under ${COMPILER_ROOT}" >&2
  exit 127
fi

driver_can_run() {
  "${CC1}" -version </dev/null >/dev/null 2>&1
}

if driver_can_run; then
  exec "${EE}" "$@"
fi

I386_LIBS="${I386}/usr/lib/i386-linux-gnu:${I386}/lib/i386-linux-gnu:${I386}/lib:${I386}/usr/lib"
if "${I386}/lib/ld-linux.so.2" --library-path "${I386_LIBS}" "${CC1}" -version >/dev/null 2>&1; then
  i386_runner=("${I386}/lib/ld-linux.so.2" --library-path "${I386_LIBS}")
elif [[ -f "${QEMU}" && -f "${I386}/lib/ld-linux.so.2" ]]; then
  i386_runner=("${QEMU}" -L "${I386}")
else
  echo "ee-gcc-qemu: neither the i386 loader nor qemu-i386 ran under ${I386}" >&2
  exit 127
fi

out=""
src=""
opt="-O2"
cc1_args=()
as_args=()
fast_math=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c)
      shift
      ;;
    -o)
      out="$2"
      shift 2
      ;;
    -Wa,*)
      IFS=',' read -r _wa rest <<< "$1"
      IFS=',' read -ra wa_parts <<< "${rest}"
      as_args+=("${wa_parts[@]}")
      shift
      ;;
    -I*|-D*|-U*|-W*)
      cc1_args+=("$1")
      shift
      ;;
    -G*)
      cc1_args+=("$1")
      as_args+=("$1")
      shift
      ;;
    -O*)
      opt="$1"
      shift
      ;;
    -g|-g*)
      cc1_args+=("$1")
      as_args+=("$1")
      shift
      ;;
    -ffast-math)
      fast_math=1
      cc1_args+=("$1")
      shift
      ;;
    -f*)
      cc1_args+=("$1")
      shift
      ;;
    --version)
      echo "2.9-ee-991111-01"
      exit 0
      ;;
    *.c|*.cpp)
      src="$1"
      shift
      ;;
    *)
      cc1_args+=("$1")
      shift
      ;;
  esac
done

if [[ -z "${src}" ]]; then
  echo "ee-gcc-qemu: no C or C++ source" >&2
  exit 2
fi
if [[ -z "${out}" ]]; then
  out="${src%.*}.o"
fi

compiler_proper="${CC1}"
lang_args=(-lang-c)
lang_defines=(-D__LANGUAGE_C -D_LANGUAGE_C -DLANGUAGE_C)
if [[ "${src}" == *.cpp ]]; then
  compiler_proper="${COMPILER_ROOT}/lib/gcc-lib/ee/${GCC_VERSION}/cc1plus"
  lang_args=(-lang-c++ -D__cplusplus)
  lang_defines=(-D__LANGUAGE_C_PLUS_PLUS -D__GNUG__=2)
fi

tmp_s="$(mktemp "${TMPDIR:-/tmp}/ee-cc1-XXXXXX.s")"
trap 'rm -f "${tmp_s}"' EXIT

defines=(-D__OPTIMIZE__)
if [[ "${fast_math}" -eq 1 ]]; then
  defines+=(-D__FAST_MATH__)
fi

"${i386_runner[@]}" "${compiler_proper}" "${src}" \
  "${lang_args[@]}" \
  ${cc1_args[@]+"${cc1_args[@]}"} \
  -iprefix "${COMPILER_ROOT}/lib/gcc-lib/ee/${GCC_VERSION}/" \
  -quiet \
  -dumpbase "$(basename "${src}")" \
  -undef \
  -D__GNUC__=2 \
  -D__GNUC_MINOR__=9 \
  -Dmips -DMIPSEL -DR5900 \
  -D_mips -D_MIPSEL -D_R5900 \
  -D__mips__ -D__MIPSEL__ -D__R5900__ \
  -D___mips__ -D_MIPSEL -D_R5900 \
  -D__mips -D__MIPSEL -D__R5900 \
  -D___mips \
  "${defines[@]}" \
  "${lang_defines[@]}" \
  "-D__SIZE_TYPE__= unsigned int" \
  "-D__PTRDIFF_TYPE__= int" \
  "-D__LONG_MAX__=9223372036854775807L" \
  -U__mips \
  -D__mips=3 \
  -D__mips64 \
  -D__mips_eabi \
  -D__mips_single_float \
  "${opt}" \
  -o "${tmp_s}"

"${i386_runner[@]}" "${AS}" \
  "${opt}" \
  -mabi=eabi \
  ${as_args[@]+"${as_args[@]}"} \
  -o "${out}" \
  "${tmp_s}"
