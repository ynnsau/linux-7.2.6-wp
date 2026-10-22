#!/bin/sh
# Executed only as PID 1 inside the disposable QEMU guest.
/bin/busybox --install -s /bin
export PATH=/tests:/bin:/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /sys/kernel/debug
mount -t debugfs debugfs /sys/kernel/debug
echo '5 4 1 7' > /proc/sys/kernel/printk
echo 30 > /proc/sys/kernel/hung_task_timeout_secs
echo CMDP_RANGE_VM_START
uname -a
suite=small
for arg in $(cat /proc/cmdline); do
    case "$arg" in cmdp_suite=*) suite=${arg#cmdp_suite=};; esac
done
rc=0
finish() {
    cat /sys/kernel/debug/cmdp/stats
    for key in entries managed isolated; do
        value=$(awk -v key="$key" '$1==key {print $2}' /sys/kernel/debug/cmdp/stats)
        test "$value" = 0 || rc=1
    done
    echo CMDP_RANGE_VM_RESULT=$rc
    sync
    /tests/console-drain || echo CMDP_CONSOLE_DRAIN_FAILED
    poweroff -f
    while :; do sleep 60; done
}
run() {
    echo "CMDP_CASE_BEGIN $*"
    "$@"
    result=$?
    echo "CMDP_CASE_END result=$result"
    if test "$result" != 0; then rc=1; finish; fi
}
# Topology is a gate, not merely an advertised QEMU command line.
test "$(cat /sys/devices/system/node/online)" = '0-1' || rc=1
test "$(cat /sys/devices/system/node/node0/cpulist)" = '0-1' || rc=1
test "$(cat /sys/devices/system/node/node1/cpulist)" = '2-3' || rc=1
test "$(cat /sys/devices/system/cpu/online)" = '0-3' || rc=1
echo "CMDP_TOPOLOGY nodes=$(cat /sys/devices/system/node/online) node0_cpus=$(cat /sys/devices/system/node/node0/cpulist) node1_cpus=$(cat /sys/devices/system/node/node1/cpulist)"
cat /sys/devices/system/node/node*/meminfo
test -w /proc/sys/kernel/numa_balancing || rc=1
test "$rc" = 0 || finish
echo 0 > /proc/sys/kernel/numa_balancing
case "$suite" in
recovery-baseline|recovery|recovery-numa)
    run /tests/recovery-vm-tests.sh
    ;;
dirty)
    run /tests/dirty-vm-tests.sh
    ;;
small)
    run /tests/cmdp_test
    run /tests/cmdp_monitor_test
    # Fixed victim size, increasing lower-address unaffected population.
    for pages in 0 4096 65536; do
        run /tests/cmdp_range_test --prefix-pages "$pages" --victim-pages 1024
    done
    # Fixed prefix, increasing victim range; then head and empty controls.
    for pages in 1 4096 16384; do
        run /tests/cmdp_range_test --prefix-pages 65536 --victim-pages "$pages"
    done
    run /tests/cmdp_range_test --prefix-pages 65536 --victim-pages 1024 --head
    run /tests/cmdp_range_test --prefix-pages 65536 --victim-pages 1024 --empty
    run /tests/cmdp_range_test --prefix-pages 4096 --victim-pages 1024 --stop-wait
    for mode in hotcold concurrent churn; do
        echo "CMDP_REPORT_BEGIN $mode"
        /tests/cmdp_monitor --period-ms 100 --max-pages 32768 --max-history 65536 \
            -- /tests/cmdp_fixture --mode "$mode" --seconds 3
        result=$?
        echo "CMDP_REPORT_END $mode result=$result"
        test "$result" = 0 || { rc=1; finish; }
    done
    export OMP_NUM_THREADS=4 OMP_PROC_BIND=close OMP_PLACES=cores
    echo 'CMDP_REPORT_BEGIN bfs'
    /tests/cmdp_monitor --period-ms 100 --max-pages 262144 --max-history 524288 \
        -- /tests/bfs -g 18 -n 8 -v > /tmp/bfs-report.jsonl 2> /tmp/bfs-target.log
    result=$?
    cat /tmp/bfs-report.jsonl
    echo "CMDP_REPORT_END bfs result=$result"
    cat /tmp/bfs-target.log
    test "$result" = 0 || rc=1
    test "$(grep -c 'Verification:.*PASS' /tmp/bfs-target.log)" = 8 || rc=1
    if grep -q 'Verification:.*FAIL' /tmp/bfs-target.log; then rc=1; fi
    ;;
scale)
    # Keep a 256 MiB victim fixed. Last case has exactly 2M fixture pages.
    for prefix in 0 196608 983040 2031616; do
        run /tests/cmdp_range_test --prefix-pages "$prefix" --victim-pages 65536
    done
    run /tests/cmdp_range_test --prefix-pages 2031616 --victim-pages 65536 --head
    run /tests/cmdp_range_test --prefix-pages 2031616 --victim-pages 65536 --empty
    ;;
numa)
    echo 1 > /proc/sys/kernel/numa_balancing
    test "$(cat /proc/sys/kernel/numa_balancing)" = 1 || { rc=1; finish; }
    echo "CMDP_NUMA_ENABLED=$(cat /proc/sys/kernel/numa_balancing)"
    run /tests/cmdp_range_test --prefix-pages 196608 --victim-pages 65536 --numa-seconds 45
    ;;
*) echo "Unknown suite: $suite"; rc=1;;
esac
finish
