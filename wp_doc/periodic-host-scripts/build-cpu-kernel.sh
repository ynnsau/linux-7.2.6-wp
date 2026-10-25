#!/bin/bash
# Build only. Preserve prior outputs and never invoke installation hooks.
set -euo pipefail
mode=${1:?debug or production}
run=${2:?new evidence directory}
target=${3:-bzImage}
case "$mode" in debug|production) ;; *) exit 2;; esac
case "$target" in bzImage|all) ;; *) exit 2;; esac
src=/home/yans3/cmdp-v2/linux-fused-scanner
seed=/home/yans3/cmdp-v2/periodic/fused-scanner-20261022
out=$run/build-$mode
if test ! -d "$out"; then
    cp -a --reflink=auto "$seed/build-$mode" "$out"
fi
"$src/scripts/config" --file "$out/.config" --disable LOCALVERSION_AUTO \
    --set-str LOCALVERSION "-cmdp-cpu-$mode"
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j32 "$target"
make -s -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease
sha256sum "$out/arch/x86/boot/bzImage" "$out/.config"
