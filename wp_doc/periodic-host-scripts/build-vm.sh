#!/bin/bash
set -euo pipefail
base=/home/yans3/cmdp-v2
src=$base/linux
out=$base/periodic/vm
mkdir -p "$out"
stage=$(mktemp -d "$out/root.XXXXXX")
mkdir -p "$stage"/{bin,sbin,etc,proc,sys,dev,tmp,tests,run}
mkdir -p "$out/headers/linux"
cp "$src/include/uapi/linux/cmdp.h" "$out/headers/linux/"
cp /bin/busybox "$stage/bin/"
ln -s busybox "$stage/bin/sh"
for test in cmdp_test cmdp_monitor cmdp_fixture cmdp_monitor_test; do
        test -f "$src/tools/testing/selftests/cmdp/$test.c"
        gcc-13 -static -O2 -Wall -Wextra -Werror -pthread \
            -I"$out/headers" \
            "$src/tools/testing/selftests/cmdp/$test.c" \
            -o "$stage/tests/$test"
done
cat > "$stage/tmp/console-drain.c" <<'DRAIN'
#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
int main(void)
{
    int fd = open("/dev/ttyS0", O_WRONLY | O_NOCTTY);
    if (fd < 0) return 1;
    int rc = tcdrain(fd);
    close(fd);
    return rc != 0;
}
DRAIN
gcc-13 -static -O2 -Wall -Wextra -Werror "$stage/tmp/console-drain.c" \
    -o "$stage/tests/console-drain"
if test -x "$base/gapbs-study/gapbs/bfs"; then
    cp "$base/gapbs-study/gapbs/bfs" "$stage/tests/bfs"
    while read -r lib; do
        mkdir -p "$stage$(dirname "$lib")"
        cp -L "$lib" "$stage$lib"
    done < <(ldd "$base/gapbs-study/gapbs/bfs" | awk '/=> \/|^[ \t]*\// { for(i=1;i<=NF;i++) if($i ~ /^\//) print $i }')
fi
cat > "$stage/init" <<'INIT'
#!/bin/sh
/bin/busybox --install -s /bin
export PATH=/tests:/bin:/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /sys/kernel/debug
mount -t debugfs debugfs /sys/kernel/debug
echo '5 4 1 7' > /proc/sys/kernel/printk
echo CMDP_VM_START
uname -a
rc=0
for program in cmdp_test cmdp_monitor_test cmdp_monitor cmdp_fixture bfs; do
    test -x "/tests/$program" || rc=1
done
test "$rc" = 0 && timeout -t 180 /tests/cmdp_test || rc=1
test "$rc" = 0 && timeout -t 240 /tests/cmdp_monitor_test || rc=1
if test "$rc" = 0; then
    for mode in hotcold concurrent churn; do
        timeout -t 30 /tests/cmdp_monitor --period-ms 100 --max-pages 32768 --max-history 65536 \
            -- /tests/cmdp_fixture --mode "$mode" --seconds 3 \
            > "/tmp/report-$mode.jsonl" 2> "/tmp/workload-$mode.log" || rc=1
        echo "CMDP_REPORT_BEGIN $mode"
        cat "/tmp/report-$mode.jsonl"
        echo "CMDP_REPORT_END $mode"
        cat "/tmp/workload-$mode.log"
    done
    if test -x /tests/bfs; then
        export OMP_NUM_THREADS=4 OMP_PROC_BIND=close OMP_PLACES=cores
        timeout -t 120 /tests/cmdp_monitor --period-ms 100 --max-pages 262144 --max-history 524288 \
            -- /tests/bfs -g 18 -n 8 -v > /tmp/report-bfs.jsonl 2> /tmp/workload-bfs.log || rc=1
        echo 'CMDP_REPORT_BEGIN bfs'
        cat /tmp/report-bfs.jsonl
        echo 'CMDP_REPORT_END bfs'
        cat /tmp/workload-bfs.log
        grep -q 'Verification:.*PASS' /tmp/workload-bfs.log || rc=1
        if grep -q 'Verification:.*FAIL' /tmp/workload-bfs.log; then rc=1; fi
    fi
fi
cat /sys/kernel/debug/cmdp/stats
echo CMDP_VM_RESULT=$rc
sync
/tests/console-drain || echo CMDP_CONSOLE_DRAIN_FAILED
poweroff -f
INIT
chmod +x "$stage/init"
(cd "$stage" && find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -1) > "$out/initramfs.cpio.gz"
printf '%s\n' "$stage" > "$out/stage-path"
sha256sum "$stage"/tests/* > "$out/test-binaries.sha256"
sha256sum "$out/initramfs.cpio.gz"
