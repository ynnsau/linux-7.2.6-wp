#!/bin/bash
set -euo pipefail
base=/home/yans3/cmdp-v2
src=$base/linux
out=$base/build
records=$base/periodic/records
test -z "$(git -C "$src" status --porcelain=v1)"
date -u +%FT%TZ > "$records/build-started"
git -C "$src" rev-parse HEAD > "$records/source-head-at-build"
git -C "$src" diff --binary > "$records/source-diff-at-build.patch"
"$src/scripts/config" --file "$out/.config" \
    --set-str LOCALVERSION '-cmdp-periodic-v1' --disable LOCALVERSION_AUTO
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= olddefconfig
make -s -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= kernelrelease \
    > "$records/kernel-release.txt"
make -C "$src" O="$out" CC=gcc-13 HOSTCC=gcc-13 LOCALVERSION= -j32 bzImage modules
test -z "$(git -C "$src" status --porcelain=v1)"
test "$(git -C "$src" rev-parse HEAD)" = "$(cat "$records/source-head-at-build")"
cp "$out/.config" "$records/config-built"
sha256sum "$out/arch/x86/boot/bzImage" "$out/vmlinux" "$out/.config" "$out/System.map" \
    > "$records/build-artifacts.sha256"
date -u +%FT%TZ > "$records/build-finished"
