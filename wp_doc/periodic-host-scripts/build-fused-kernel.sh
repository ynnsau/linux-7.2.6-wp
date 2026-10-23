#!/bin/bash
# Build only; no host installation, GRUB update, or reboot.
set -euo pipefail
mode=${1:?debug or production}
target=${2:-bzImage}
case "$mode" in debug|production) ;; *) exit 2;; esac
case "$target" in bzImage|all) ;; *) exit 2;; esac
base=/home/yans3/cmdp-v2
src=$base/linux-fused-scanner
run=$base/periodic/fused-scanner-20261022
seed=$base/periodic/batch-arm-20261022
out=$run/build-$mode
mkdir -p "$run"
if test ! -d "$out"; then
    cp -a "$seed/build-$mode" "$out"
fi
"$src/scripts/config" --file "$out/.config" --disable LOCALVERSION_AUTO \
    --set-str LOCALVERSION "-cmdp-fused-$mode"
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j32 "$target"
make -s -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease
sha256sum "$out/arch/x86/boot/bzImage" "$out/.config"
