#!/bin/sh
# Guest-only: disposable virtual disk supplied by run-range-vm.sh dirty.
set -eu
test "$$" -ne 1
# Refuse accidental invocation on a normal host, even if it has a /dev/vda.
cmdline=" $(cat /proc/cmdline) "
case "$cmdline" in *" cmdp_suite=dirty "*) ;; *) exit 2;; esac
case "$cmdline" in *" cmdp_disposable_swap=1 "*) ;; *) exit 2;; esac
test -b /dev/vda
test "$(cat /sys/block/vda/size)" = 524288
mkswap /dev/vda
swapon /dev/vda
grep -q '^/dev/vda ' /proc/swaps
echo CMDP_DIRTY_SWAP_READY
cat /proc/swaps
/tests/cmdp_test
/tests/cmdp_monitor_test
/tests/cmdp_range_test --prefix-pages 4096 --victim-pages 1024
/tests/cmdp_range_test --prefix-pages 4096 --victim-pages 1024 --stop-wait
/tests/cmdp_dirty_test
export OMP_PROC_BIND=close OMP_PLACES=cores
for mode in observe qualify; do
    for threads in 1 4; do
        export OMP_NUM_THREADS=$threads
        name=$mode-bfs$threads
        echo "CMDP_REPORT_BEGIN $name"
        set +e
        /tests/cmdp_monitor --dirty-$mode --quiet-ms 10 --period-ms 100 \
            --poll-ms 1000 --max-pages 262144 --max-history 524288 \
            -- /tests/bfs -g 18 -n 8 -v > /tmp/bfs-report.jsonl 2> /tmp/bfs-target.log
        result=$?
        set -e
        # Repeated cohort snapshots are redundant; retain status, observation
        # boundaries, sweep completions, and every final cohort for validation.
        grep -v '"event":"cohort_snapshot"\|"event":"cohort_observation_end"' /tmp/bfs-report.jsonl
        echo "CMDP_REPORT_END $name result=$result"
        cat /tmp/bfs-target.log
        test "$result" = 0
        test "$(grep -c 'Verification:.*PASS' /tmp/bfs-target.log)" = 8
        if grep -q 'Verification:.*FAIL' /tmp/bfs-target.log; then exit 1; fi
        grep -q '"event":"dirty_status"' /tmp/bfs-report.jsonl
        samples=$(grep '"event":"dirty_status"' /tmp/bfs-report.jsonl | tail -1 | sed 's/.*"samples":\([0-9][0-9]*\).*/\1/')
        test "$samples" -gt 0
        arms=$(grep '"event":"final"' /tmp/bfs-report.jsonl | sed 's/.*"first_arms":\([0-9][0-9]*\).*/\1/')
        case "$mode" in observe) test "$arms" = 0;; qualify) test "$arms" -gt 0;; esac
        echo "CMDP_DIRTY_BFS mode=$mode threads=$threads samples=$samples first_arms=$arms verified=8"
    done
done
swapoff /dev/vda
echo CMDP_DIRTY_SWAP_DRAINED
