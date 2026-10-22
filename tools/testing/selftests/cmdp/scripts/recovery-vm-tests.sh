#!/bin/sh
# Guest-only deterministic before/after regression and generated workload gates.
set -eu
cmdline=" $(cat /proc/cmdline) "
case "$cmdline" in
*" cmdp_suite=recovery-baseline "*)
    for threads in 1 4; do
        /tests/cmdp_recovery_test --threads "$threads" --expect-stranded
    done
    exit 0
    ;;
*" cmdp_suite=recovery-numa "*)
    # Shorten only guest NUMA scan intervals; host settings remain untouched.
    # Do not report success if actual NUMA source callbacks were not observed.
    echo 1 > /proc/sys/kernel/numa_balancing
    echo 100 > /sys/kernel/debug/sched/numa_balancing/scan_delay_ms
    echo 100 > /sys/kernel/debug/sched/numa_balancing/scan_period_min_ms
    echo 1000 > /sys/kernel/debug/sched/numa_balancing/scan_period_max_ms
    echo 64 > /sys/kernel/debug/sched/numa_balancing/scan_size_mb
    echo CMDP_RECOVERY_NUMA_CONFIG
    for f in /proc/sys/kernel/numa_balancing /sys/kernel/debug/sched/numa_balancing/scan_*; do echo "$f=$(cat "$f")"; done
    for threads in 1 4; do
        /tests/cmdp_recovery_test --threads "$threads" --numa-seconds 15
        /tests/cmdp_recovery_test --threads "$threads" --numa-seconds 15 --cross-node
    done
    echo 0 > /proc/sys/kernel/numa_balancing
    exit 0
    ;;
*" cmdp_suite=recovery "*) ;;
*) echo 'Refusing recovery suite outside designated disposable guest' >&2; exit 2;;
esac
for threads in 1 4; do /tests/cmdp_recovery_test --threads "$threads"; done
test -x /tests/pr
export OMP_PROC_BIND=close OMP_PLACES=cores OMP_DYNAMIC=FALSE
for application in bfs pr; do
    for threads in 1 4; do
        export OMP_NUM_THREADS=$threads
        name=recovery-$application$threads
        echo "CMDP_REPORT_BEGIN $name"
        case "$application" in
            bfs) set -- /tests/bfs -g 18 -n 8 -v;;
            pr) set -- /tests/pr -g 18 -n 8 -i 1000 -t 1e-4 -v;;
        esac
        set +e
        /tests/cmdp_monitor --dirty-qualify --quiet-ms 10 --period-ms 100 \
            --poll-ms 1000 --max-pages 262144 --max-history 524288 \
            -- "$@" > /tmp/workload-report.jsonl 2> /tmp/workload-target.log
        result=$?
        set -e
        grep -v '"event":"cohort_snapshot"\|"event":"cohort_observation_end"' /tmp/workload-report.jsonl
        echo "CMDP_REPORT_END $name result=$result"
        cat /tmp/workload-target.log
        test "$result" = 0
        test "$(grep -c 'Verification:.*PASS' /tmp/workload-target.log)" = 8
        if grep -q 'Verification:.*FAIL' /tmp/workload-target.log; then exit 1; fi
        grep -q '"event":"dirty_status"' /tmp/workload-report.jsonl
        samples=$(grep '"event":"dirty_status"' /tmp/workload-report.jsonl | tail -1 | sed 's/.*"samples":\([0-9][0-9]*\).*/\1/')
        test "$samples" -gt 0
        echo "CMDP_RECOVERY_WORKLOAD app=$application threads=$threads samples=$samples verified=8"
    done
done
