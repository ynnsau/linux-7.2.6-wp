# CMD-P v1 software-path benchmark

This benchmark measures the current dummy-backend software prototype. It does
not measure real CXL/CMD-P hardware, device coherence, or device drain latency.

## Method

`cmdp_bench` pins itself to the first allowed guest CPU (vCPU 0 here), warms up
the arm/fault path, and collects 1,000 samples by default. It reports TSC ticks,
not CPU cycles. Each timestamp pair uses `LFENCE; RDTSC` before the operation
and `RDTSCP; LFENCE` after it, with compiler memory barriers. CPU affinity and
the RDTSCP auxiliary CPU value are checked for migration. A separate empty
timestamp-pair distribution estimates timer overhead.

The ordinary-store sample times a block of 256 volatile stores and normalizes
by 256 to reduce timestamp overhead. For each CMD-P iteration, the benchmark
reads the debugfs counters outside the timed interval, issues ARM, samples one
store into the armed page, and checks that exactly one revoke occurred and that
`entries`, `managed`, and `isolated` returned to zero. Kernel arm/revoke values
are per-iteration deltas of `arm_ns` and `revoke_ns`, reported in nanoseconds.
Tracing is enabled for the preceding selftests and disabled during timing.
`CLOCK_MONOTONIC_RAW` times a long baseline-store loop and the complete measured
loop; the latter includes counter reads and is only a sanity reference.

## Environment

- QEMU: `qemu-system-x86_64 -machine q35,accel=kvm -cpu host -m 2048 -smp 2`
- Guest kernel: Linux 7.2.6 x86-64, `CONFIG_CMDP=y`, `CONFIG_CMDP_TEST=y`,
  `CONFIG_DEBUG_FS=y`, built with `W=1` and `CONFIG_WERROR=y`
- Page size: 4 KiB
- Benchmark affinity: guest CPU 0; one benchmark thread
- Samples: 1,000 per reported distribution

## Results

One QEMU run produced the following measurements:

| Operation | Unit | Min | Median | p95 | p99 | Max |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Empty timestamp pair | TSC ticks/pair | 20 | 40 | 40 | 40 | 40 |
| Ordinary store | TSC ticks/store | 3.98 | 4.06 | 4.06 | 5.08 | 286.48 |
| `CMDP_IOC_ARM` end-to-end | TSC ticks/op | 6,940 | 7,880 | 17,080 | 25,880 | 282,000 |
| First store and revoke | TSC ticks/op | 4,260 | 4,800 | 8,620 | 10,160 | 283,920 |
| Kernel arm counter delta | ns/op | 2,846 | 3,226 | 7,143 | 9,117 | 38,693 |
| Kernel revoke counter delta | ns/op | 711 | 801 | 1,343 | 1,623 | 58,321 |

The same run reported `CLOCK_MONOTONIC_RAW ordinary_store_avg_ns=2.32` and
`cmdp_full_loop_avg_ns=37881`; the latter includes debugfs counter reads. The
kernel counters ended at `entries=0 managed=0 isolated=0`. Large maximum values
reflect guest scheduling/interference. These results describe only the
software/prototype path and are not representative of final physical CMD-P
hardware performance.

## Validation

The normal QEMU run passed all 14 CMD-P selftests, including invalid and
foreign-mm ioctl checks, concurrent writers, repeated cycles, munmap, and
process-exit cleanup. Final manager counters were zero for `entries`, `managed`,
and `isolated`.

A separate `W=1`, `CONFIG_WERROR=y` kernel with `CONFIG_LOCKDEP=y`,
`CONFIG_PROVE_LOCKING=y`, `CONFIG_DEBUG_LOCK_ALLOC=y`, and
`CONFIG_DEBUG_ATOMIC_SLEEP=y` booted in QEMU and passed the same 14 tests.
The lockdep guest finished with `entries=0 managed=0 isolated=0`; no lockdep,
atomic-sleep, refcount, hung-task, or CMD-P warning was reported.
