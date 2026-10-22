#!/bin/bash
# Build a disposable initramfs; no host kernel/module installation.
set -euo pipefail
src=${1:?Usage: build-range-vm.sh SOURCE NEW_OUTPUT_DIR BFS_BINARY [PR_BINARY]}
out=${2:?A new output directory is required}
bfs=${3:?A verified GAPBS BFS binary is required}
pr=${4:-}
cc=${CC:-gcc-13}
busybox=${BUSYBOX:-/bin/busybox}
test ! -e "$out" || { echo "Refusing existing output directory: $out" >&2; exit 2; }
test -x "$busybox" && test -x "$bfs"
mkdir -p "$out"
out=$(cd "$out" && pwd)
src=$(cd "$src" && pwd)
stage=$out/root
mkdir -p "$stage"/{bin,sbin,etc,proc,sys,dev,tmp,tests,run} "$out/headers/linux"
cp "$src/include/uapi/linux/cmdp.h" "$out/headers/linux/"
cp "$busybox" "$stage/bin/busybox"
ln -s busybox "$stage/bin/sh"
for name in cmdp_test cmdp_monitor_test cmdp_range_test cmdp_dirty_test cmdp_recovery_test cmdp_batch_test cmdp_monitor cmdp_fixture; do
    "$cc" -static -O2 -Wall -Wextra -Werror -pthread -I"$out/headers" \
        "$src/tools/testing/selftests/cmdp/$name.c" -o "$stage/tests/$name"
done
cp "$bfs" "$stage/tests/bfs"
binaries=("$bfs")
if test -n "$pr"; then
    test -x "$pr"
    cp "$pr" "$stage/tests/pr"
    binaries+=("$pr")
fi
while read -r lib; do
    mkdir -p "$stage$(dirname "$lib")"
    cp -L "$lib" "$stage$lib"
done < <(for binary in "${binaries[@]}"; do ldd "$binary"; done |
    awk '/=> \/|^[ \t]*\// { for(i=1;i<=NF;i++) if($i ~ /^\//) print $i }' | sort -u)
cat > "$out/console-drain.c" <<'DRAIN'
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
int main(void) {
    int fd = open("/dev/ttyS0", O_WRONLY | O_NOCTTY);
    if (fd < 0) return 1;
    int rc = tcdrain(fd); close(fd); return rc != 0;
}
DRAIN
"$cc" -static -O2 -Wall -Wextra -Werror "$out/console-drain.c" -o "$stage/tests/console-drain"
cp "$src/tools/testing/selftests/cmdp/scripts/range-vm-init.sh" "$stage/init"
cp "$src/tools/testing/selftests/cmdp/scripts/dirty-vm-tests.sh" "$stage/tests/dirty-vm-tests.sh"
cp "$src/tools/testing/selftests/cmdp/scripts/recovery-vm-tests.sh" "$stage/tests/recovery-vm-tests.sh"
chmod +x "$stage/tests/dirty-vm-tests.sh" "$stage/tests/recovery-vm-tests.sh"
chmod +x "$stage/init"
(cd "$stage" && find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -1) > "$out/initramfs.cpio.gz"
sha256sum "$stage"/tests/* "$out/initramfs.cpio.gz" > "$out/inputs.sha256"
sha256sum "$src/mm/cmdp.c" "$src/include/uapi/linux/cmdp.h" \
    "$src/tools/testing/selftests/cmdp/"*.c \
    "$src/tools/testing/selftests/cmdp/scripts/"*.sh \
    "$src/tools/testing/selftests/cmdp/scripts/"*.py > "$out/source-files.sha256"
printf '%s\n' "$src" > "$out/source-path"
git -C "$src" rev-parse HEAD > "$out/source-commit"
git -C "$src" diff --binary > "$out/source-working-tree.patch"
cat "$out/inputs.sha256"
