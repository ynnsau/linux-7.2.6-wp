#!/bin/bash
# Build a recovery candidate only; never install, update GRUB or reboot.
set -euo pipefail
mode=${1:?debug or production}
target=${2:-bzImage}
case "$mode" in debug|production) ;; *) exit 2;; esac
case "$target" in bzImage|all) ;; *) exit 2;; esac
base=${CMDP_BASE:-/home/yans3/cmdp-v2}
src=${CMDP_SOURCE:-$base/linux-rearm-recovery}
run=${CMDP_RUN:-$base/periodic/rearm-recovery-20261021}
seed=${CMDP_SEED_RUN:-$base/periodic/dirty-arm-20261021}
output=$run/build-$mode
jobs=${CMDP_BUILD_JOBS:-16}
[[ $jobs =~ ^[0-9]+$ ]] && ((jobs >= 1 && jobs <= 52))
mkdir -p "$output"
if [[ ! -f "$output/.config" ]]; then
    cp "$seed/build-$mode/.config" "$output/.config"
fi
"$src/scripts/config" --file "$output/.config" \
    --enable CMDP --enable CMDP_TEST --enable DEBUG_FS \
    --enable KPROBES --enable KPROBE_EVENTS \
    --enable NUMA --enable NUMA_BALANCING --enable NUMA_BALANCING_DEFAULT_ENABLED \
    --enable DEVTMPFS --enable DEVTMPFS_MOUNT --enable BLK_DEV_INITRD \
    --enable RD_GZIP --enable SERIAL_8250_CONSOLE --enable SMP \
    --enable VIRTIO --enable VIRTIO_PCI --enable VIRTIO_BLK --enable SWAP \
    --enable CHECKPOINT_RESTORE --enable MEM_SOFT_DIRTY --enable PROC_PAGE_MONITOR \
    --disable LOCALVERSION_AUTO --set-str LOCALVERSION "-cmdp-rearm-$mode"
if [[ "$mode" == debug ]]; then
    "$src/scripts/config" --file "$output/.config" --enable PROVE_LOCKING \
        --enable DEBUG_ATOMIC_SLEEP --enable DEBUG_VM --disable DEBUG_INFO \
        --enable DEBUG_INFO_NONE
fi
make -C "$src" O="$output" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$output" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j"$jobs" "$target"
make -s -C "$src" O="$output" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease
sha256sum "$output/arch/x86/boot/bzImage" "$output/.config"
