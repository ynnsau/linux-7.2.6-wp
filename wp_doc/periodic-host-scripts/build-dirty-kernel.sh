#!/bin/bash
# Build only: never installs, modifies GRUB, or reboots the host.
set -euo pipefail
mode=${1:?debug or production}
target=${2:-bzImage}
case "$mode" in debug|production) ;; *) exit 2;; esac
case "$target" in bzImage|all) ;; *) exit 2;; esac
base=${CMDP_BASE:-/home/yans3/cmdp-v2}
src=${CMDP_SOURCE:-$base/linux-dirty-arm}
run=${CMDP_RUN:-$base/periodic/dirty-arm-20261021}
out=$run/build-$mode
mkdir -p "$out"
if [[ ! -f "$out/.config" ]]; then
    cp "$base/periodic/range-fix/build-$mode/.config" "$out/.config"
fi
"$src/scripts/config" --file "$out/.config" \
    --enable CMDP --enable CMDP_TEST --enable DEBUG_FS \
    --enable KPROBES --enable KPROBE_EVENTS \
    --enable NUMA --enable NUMA_BALANCING --enable NUMA_BALANCING_DEFAULT_ENABLED \
    --enable DEVTMPFS --enable DEVTMPFS_MOUNT --enable BLK_DEV_INITRD \
    --enable RD_GZIP --enable SERIAL_8250_CONSOLE --enable SMP \
    --enable VIRTIO --enable VIRTIO_PCI --enable VIRTIO_BLK --enable SWAP \
    --enable CHECKPOINT_RESTORE --enable MEM_SOFT_DIRTY --enable PROC_PAGE_MONITOR \
    --disable LOCALVERSION_AUTO --set-str LOCALVERSION "-cmdp-dirty-$mode"
if [[ "$mode" == debug ]]; then
    "$src/scripts/config" --file "$out/.config" --enable PROVE_LOCKING \
        --enable DEBUG_ATOMIC_SLEEP --enable DEBUG_VM --disable DEBUG_INFO \
        --enable DEBUG_INFO_NONE
fi
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j16 "$target"
make -s -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease
sha256sum "$out/arch/x86/boot/bzImage" "$out/.config"
