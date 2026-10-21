#!/bin/bash
set -euo pipefail
base=/home/yans3/cmdp-v2
src=$base/linux
out=$base/periodic/build-debug
mkdir -p "$out"
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 x86_64_defconfig
"$src/scripts/config" --file "$out/.config" \
    --enable CMDP --enable CMDP_TEST --enable DEBUG_FS \
    --enable PROVE_LOCKING --enable DEBUG_ATOMIC_SLEEP --enable DEBUG_VM \
    --enable DEVTMPFS --enable DEVTMPFS_MOUNT --enable BLK_DEV_INITRD \
    --enable RD_GZIP --enable SERIAL_8250_CONSOLE --enable SMP \
    --enable VIRTIO_PCI --enable VIRTIO_BLK \
    --enable WERROR --disable DEBUG_INFO --enable DEBUG_INFO_NONE \
    --disable LOCALVERSION_AUTO --set-str LOCALVERSION '-cmdp-periodic-debug'
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j32 bzImage
