# Userspace measurement boundaries and frequency counting

These additions use the existing kernel interfaces. They do not modify the
kernel, frequency policies, or target-process affinity.

Build and test without root:

```sh
scripts/build-measurement-support.sh /absolute/path/to/bin
python3 scripts/test-monitor-measurement.py /absolute/path/to/bin/cmdp_monitor \
  --output /absolute/path/to/monitor-regression.json
```

The build produces `cmdp_monitor`, `frequency_collect`, and `counter_support_test`.
Compiler warnings are errors. The unit test exercises fragmented input, command
limits, malformed input, EOF, CPU ranges, and group-counter ID validation.

## Boundary commands

Both collectors accept `--control-fd FD` for an inherited readable pipe:

```text
snapshot measurement_start
snapshot measurement_end
```

Each command ends with a newline. Labels contain 1–96 ASCII letters, digits,
underscores, periods, colons, or hyphens. The parser handles partial and combined
pipe writes. Malformed commands produce a diagnostic and nonzero exit. EOF
disables the command descriptor without stopping collection or spinning.

A command promptly wakes the poll loop. It does not reset the periodic reporting
deadline. Output contains an ordered group:

1. `boundary_begin`, with `label`, `monotonic_ns`, `realtime_ns`,
   `anchor_end_ns`, and `ok`.
2. The requested observations.
3. `boundary_end`, with the same anchor fields and success status.

Each realtime anchor was read between its monotonic and anchor-end timestamps.
Different counters are sequential observations, not one atomic global snapshot.
Retain their timestamps and the group span when aligning measurement windows.
The caller should continuously drain JSONL stdout, await the matching successful
boundary-end record, and reject incomplete groups.

For `cmdp_monitor`, a group contains `boundary_status`, CPU status, batch status,
dirty status, diagnostics, and progress. Each retains its existing kernel
`now_ns`. Flush totals come from `batch_status.flush_calls`; shared flushes must
not be counted again in dirty/ARM role totals. ARM events are first arms plus
rearms, and WP events are `terminals.write` deltas.

Attach to an initialized application with `--pid PID`. The control interface is
not supported with `--monitor-off` or `--once`. A normal SIGTERM stops and drains
monitoring and returns 143; attachment does not signal the target application.
Check the final status for zero active ownership and resource/report failures
before considering that exit successful. Record measurement-end before sending
SIGTERM so STOP cleanup is outside the reported window.

## Frequency collector

```sh
sudo /absolute/path/to/bin/frequency_collect --observer-cpu 24 \
  --interval-ms 11000 --control-fd 0
```

The process pins itself once to the observer CPU. It discovers the MSR PMU type
and APERF, MPERF, and TSC event configurations from sysfs. It opens one pinned
three-event group on every online CPU and reads each group in one `read()`.
There is no overflow sampling, ring buffer, or thread migration between CPUs.
Remote counter reads still require kernel work/interrupts on other CPUs; use the
same collection schedule for every compared mode and measure its effect.

`frequency_config` records the online CPU list, observer CPU, interval, PMU type,
and event configurations. Each `frequency` row provides:

- `seq`: one full traversal's sequence number.
- `label`: the requested boundary label, or empty for periodic observations.
- `cpu`, cumulative `aperf`, `mperf`, `tsc`, `time_enabled`, `time_running`.
- `monotonic_ns`, `realtime_ns`, and `read_end_ns`, bracketing the grouped read.

The collector rejects missing counters, duplicate/unknown event IDs,
multiplexing, short reads, and changes to the online CPU set. A zero MPERF
delta is permitted; it indicates that the corresponding interval has no usable
busy-frequency estimate. Initial and final labeled samples are automatic.
`--duration-sec N` is available for short validation. SIGTERM records a final
sample, closes every counter, and exits zero if collection succeeded.

For consecutive samples of the same CPU, calculate TSC rate from the TSC delta
and elapsed monotonic read-midpoint time. Estimated busy frequency is TSC rate
times the APERF/MPERF delta ratio. Skip intervals with zero MPERF delta and retain
busy-time estimates when deciding which intervals are eligible for validation.
Compare against turbostat under the same scalar load before relying on the
estimates for this experimental CPU. The counters measure average frequency
over an interval, not instantaneous clock stability.

## Privileged integration check

Run after frequency qualification is idle; this is a small fixture and does not
change frequency settings:

```sh
sudo python3 scripts/test-measurement-boundaries.py \
  --bin-dir /absolute/path/to/bin --fixture /absolute/path/to/cmdp_fixture \
  --output /absolute/path/to/boundary-test
```

It runs a 100-page, non-writing fixture on CPU1 with THP disabled, attaches the
monitor on scanner CPU0, and checks both collectors. Fragmented commands and
two consecutive snapshots must finish within one second despite a 60-second
periodic interval. The test verifies timestamp bounds, complete CPU groups,
EOF behavior, clean STOP, and that attach-mode termination leaves the fixture
alive. JSONL evidence, stderr, and the summary remain in the output directory.
