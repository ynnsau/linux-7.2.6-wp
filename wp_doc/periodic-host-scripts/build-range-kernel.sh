#!/bin/bash
set -euo pipefail
mode=${1:?debug or production}
case "$mode" in debug|production) ;; *) exit 2;; esac
base=${CMDP_BASE:-/home/yans3/cmdp-v2}
src=${CMDP_SOURCE:-$base/linux-range-fix}
run=${CMDP_RUN:-$base/periodic/range-fix}
out=$run/build-$mode
# Start from the archived debug or host configuration. No installation step.
mkdir -p "$out"
if [[ ! -f "$out/.config" ]]; then
    if [[ "$mode" == debug ]]; then
        cp "$base/periodic/build-debug/.config" "$out/.config"
    else
        cp "$base/build/.config" "$out/.config"
    fi
fi
"$src/scripts/config" --file "$out/.config" \
    --enable CMDP --enable CMDP_TEST --enable DEBUG_FS \
    --enable KPROBES --enable KPROBE_EVENTS \
    --enable NUMA --enable NUMA_BALANCING --enable NUMA_BALANCING_DEFAULT_ENABLED \
    --enable DEVTMPFS --enable DEVTMPFS_MOUNT --enable BLK_DEV_INITRD \
    --enable RD_GZIP --enable SERIAL_8250_CONSOLE --enable SMP \
    --disable LOCALVERSION_AUTO --set-str LOCALVERSION "-cmdp-range-$mode"
if [[ "$mode" == debug ]]; then
    "$src/scripts/config" --file "$out/.config" --enable PROVE_LOCKING \
        --enable DEBUG_ATOMIC_SLEEP --enable DEBUG_VM --disable DEBUG_INFO \
        --enable DEBUG_INFO_NONE
fi
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j16 bzImage
make -s -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease
sha256sum "$out/arch/x86/boot/bzImage" "$out/.config"
