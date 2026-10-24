# BFS overhead attribution on the fused production kernel

The remaining BFS slowdown includes directly observed competition between the
scanner and BFS on CPU 0, repeated per-page timing/locking, and ownership
setup/revocation. Generic cache counters do not establish tree-specific cache
conflicts. TLB flush counts increase far more than completed page walks; this
does not provide an exclusive TLB fraction of runtime slowdown.

Completed **72 retained runs**, three rotated repetitions per thread/mode in
each of P0 reference, P1 scheduling, P2 hardware-cycle sampling, and P3 TLB/PMU
collection. Twitter BFS uses 38/127 trials on 1/4 threads, 512/512 caps, and
10-second sweep/quiet intervals. Complete-job timing includes loading,
initialization, all trials, teardown and STOP. No kernel or manual host-policy change; perf automatically reduced its global sampling ceiling, documented below.

## Unprofiled application overhead

| Threads | Off runtime (s) | Sampling overhead | Dirty → ARM overhead | Mean live ARM (GiB) | ARM / RSS exposure |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 120.94 | **7.11%** (6.98–7.14%) | **7.95%** (7.44–8.24%) | 8.12 | 70.0% |
| 4 | 120.58 | **4.17%** (3.89–4.43%) | **7.98%** (6.92–8.08%) | 5.64 | 48.6% |

Percentages are median paired changes and three-run ranges. RSS exposure is
an approximate integral of one-second observations. Peak application RSS is
about 12.3 GiB and excludes kernel metadata. Each monitored ownership entry is
960 bytes in a 1 KiB allocation; histories are 160 bytes each. Observed peak
ownership-slot estimates are 2.96/2.26 GiB plus roughly 0.48 GiB of history.
These are component estimates, excluding fragmentation and other allocations.
Tracked WP hooks total only 1.5–7.4 microseconds per P0 dirty→ARM run.

## Attribution

| Threads, mode | BFS runnable delay (s) | Scanner while BFS runnable on same core (s) | BFS mmap/rwsem blocked (s) |
| --- | ---: | ---: | ---: |
| 1, Sampling | 8.67 | 8.48 | 0.015 |
| 1, Dirty → ARM | 7.36 | 7.17 | 0.025 |
| 4, Sampling | 9.33 | 8.97 | 0.077 |
| 4, Dirty → ARM | 12.23 | 11.84 | 0.230 |

P1 values are medians and summed thread-seconds, not wall-time components.
Four-thread sleeping is mostly OpenMP barriers. Sampling-only scanner cycle
samples put 19.6%/16.6% in time reads and 15.6%/15.1% in IRQ-saving lock
acquisition. Spin slow paths are small; frequent fast paths still cost.
Dirty→ARM preparation has 42.1%/38.2% inclusive scanner cycle weight, including
allocation, reverse mapping and indexing. Inlined tree insertion alone has
8.3%/7.6% leaf weight. These nested percentages are not additive.

P0 sampling counts fall from 38.0M to 12.1M (1 thread) and 32.0M to 15.6M
(4 threads) when moving from sampling-only to dirty→ARM. Therefore C−B is a
net policy difference, not gross ARM cost. NUMA-triggered revocation and unmap
also execute synchronously in BFS; callback elapsed timers include waiting
and overlap other measurements. One P1 C1 outlier coincided with NUMA churn.

| Threads, mode | Instructions (billions) | Cache misses (millions) | Load page walks (millions) | Actual full-context flush events |
| --- | ---: | ---: | ---: | ---: |
| 1, Off | 265.1 | 5406.5 | 1301.5 | 701 |
| 1, Sampling | 315.8 | 5561.1 | 1304.7 | 1,115 |
| 1, Dirty → ARM | 307.7 | 5504.9 | 1304.1 | 2,267 |
| 4, Off | 863.4 | 17393.1 | 4139.9 | 5,072 |
| 4, Sampling | 913.1 | 17570.6 | 4215.1 | 95,303 |
| 4, Dirty → ARM | 930.6 | 17598.2 | 4220.1 | 334,427 |

P3 counters are system-wide on application CPUs, including scanner/background
work, with a short bracketing margin. Minimum running percentage: 100.00%.
Actual task-switch flushes are included; no-op switches and IPI sends are not
counted as full-context flushes. Generic counts do not identify which data
structure caused a miss. Direct batch-flush timers exclude deferred switches
and later refills. Profiled runtime variation is kept separate from P0.

## Evidence and follow-ups

Kernel `7.2.6-cmdp-fused-production`; source
`fc30294eacbc215b6bc2c0dc11e72f120c5d71d5`; build ID
`f7e8b245ab7c51144ad8a7445afe80e4b73da8ec`; unchanged boot
`04bcabc8-2c68-4e2d-8546-fdcff93a21cd`.

BMP evidence: `/home/yans3/cmdp-v2/periodic/bfs-overhead-profile-20261024-vN`.
Retain P0 from v1, P1 from v3, P2 from v4, and P3 from v5. Source commits:
`15d66c443`, `c04cc2930`, `54b990820`, `5457ce1a2`, respectively. The local study
archive uses `run`, `run-v3`, `run-v4`, `run-v5`. Earlier pilots are preserved
and excluded. Transitions were deliberate completed-job/block stops.

Every retained raw/decoded evidence hash passed verification. All 54 profiles
have complete scanner intervals and zero lost/throttled samples. Independent
controller counters match decoded callbacks, revocations, histories and
timers. Guarded runs report no new kernel warning or leaked CMD-P ownership.
Installed perf 5.4.233 and the matching vmlinux were sufficient. Perf automatically
lowered its global sampling ceiling from 63,750 to 20,250/s during the hardware
fixture and first P2 baseline. Requested frequency remained 99 Hz, below all
observed ceilings, and no retained recording has lost/throttled records. The
ceiling is left under kernel control; NUMA/THP/placement/governors are unchanged.

Prioritize the separately approved dedicated-scanner-core comparison, then a
matched NUMA-policy comparison with fresh A/B/C controls and restoration.
Later optimizations include cheaper accounting, compact single-alias ownership,
bounded revocation, and 10→20→40-second adaptive revisits with fresh qualification
after backoff. Keep final safety checks and substantial ARM coverage.

The [protocol](bfs-overhead-profile-protocol.md) records the frozen design.
Analysis helpers are in `tools/testing/selftests/cmdp/scripts/`:
`analyze-bfs-overhead-profile.py`, `validate-profile-analysis.py`,
`verify-profile-archive.py`, `summarize-profile.py`, `report-profile.py`,
and source/counter summaries. Raw-header and synthetic scheduling/sample tests
are `test-perf-record-audit.py` and `test-profile-analysis.py`.
Plots, detailed per-run JSON, raw recordings and source/layout maps remain in
the study's `results/20261024-bfs-overhead-profile/` archive and on BMP.
