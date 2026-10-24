# Initial BFS overhead attribution

Retain the measured fused production kernel and unchanged complete-job timing
executor. Reuse verified native tools from the completed batch sweep. Twitter
BFS: 38 trials on one thread, 127 on four threads; batch ceilings 512/512;
period and quiet interval 10 seconds. Three rotated A/B/C repetitions per block:
A monitoring off, B sampling only, C dirty qualification plus ARM.

Four blocks (72 measured jobs): P0 reference, P1 scheduler/workqueue/write-mmap and
coarse invalidation events, P2 99-Hz CPU call chains plus workqueue/invalidation
context, P3 small hardware counter group plus TLB/workqueue/invalidation events.
Do not record per-page CMD-P events. Start and stop collectors outside the
unchanged launcher timer. Perf collectors use CPUs 24–25; BFS keeps CPU 0 or
CPUs 0–3. Source, binaries, kernel, boot identity and host policy are guarded.

The installed perf 5.4.233 executable works directly; the Ubuntu /usr/bin/perf
wrapper does not recognize this custom kernel. Use the existing matching
vmlinux for symbols. No installation, kernel changes, GRUB changes or reboot.

P3 uses system-wide per-CPU one-second intervals for cycles, instructions,
cache misses, completed load page walks and completed store page walks. This
is a three-programmable-counter group plus two fixed counters, verified without
multiplexing. CPU totals include background/scanner execution; do not describe
them as task-exclusive counters or attribute cache misses to the address tree.

Keep profiler-on elapsed results separate from P0. Preserve raw perf.data,
decoded gzip streams, tool logs, commands and collection boundaries. Reject
recording loss/PMU multiplexing below 95%; review profiler perturbation before
causal conclusions. Scheduling trace state and work-item identity distinguish
runnable delay from sleeping and scanner work from unrelated kworker use.

The two approved follow-ups (spare physical scanner core, conditional NUMA
balancing comparison) remain separate experiments after initial attribution.
Current code has no dedicated scanner placement control; do not pin shared
kworkers. Adaptive candidate backoff remains a later optimization.

## Capture refinement after the first trace pilot

The first all-mmap-lock trace recorded about 9.1 million samples even with
monitoring off, largely ordinary fault-path read locks. Its 123.95-second
baseline and excessive recording/decoding volume justify narrowing collection.
Preserve the completed 18-run P0 block and the discarded broad trace pilot.
Continue P1--P3 in a separate evidence directory, with write == 1 on all mmap
lock tracepoints. Scheduler switch stacks still identify read-lock blocking;
per-page read-lock acquisitions are not recorded. Validate the 18 reference
receipts plus kernel/binary/tool identity before linking the campaigns.

## Collector readiness and clock validation

Before launching the timed job, require all expected per-CPU event descriptors,
a nonempty recording file, and one second of settling. Checking only the first
perf descriptor missed an initial scanner-start marker in a discarded pilot.
Use CLOCK_MONOTONIC timestamps and retain raw controller FORK/EXIT records for
exact boundaries in each recording. Do not compare different perf clock epochs.

The discarded CPU-clock P2 pilots sampled active work every 10,101,010 ns
(approximately 99 Hz) with frame-pointer call chains. Those events excluded
idle tasks (`/I`): idle software-clock
interrupts produced throttling in the short validation fixture, consistent
with the idle scheduler-tick behavior. The measured kernel's hrtimer callback checks
exclude_idle before accounting a sample. No global perf throttling or tick
policy is changed. Keep context tracepoints at their default period of one;
do not apply a global sample frequency to them. Verify complete scanner work
markers and zero lost/throttled samples before the full campaign.

## Retained hardware-cycle profiles

The three initial P2 CPU-clock profiles are diagnostic pilots. Many scanner
samples landed at `_raw_spin_unlock_irqrestore`; timer interrupts can be
deferred until interrupts are restored, so this is not evidence that the
unlock instruction itself consumes that time or that a lock is contended.
The CPU-clock hrtimer path and the x86 PMU NMI path differ in this respect.

Retain P0 from v1 and all 18 P1 profiles from v3. Collect P2 in v4 after
validating those source/kernel/tool identities and every reused receipt. P2
uses `cycles/freq=99,call-graph=fp/` with context tracepoints still at period
one. Report sampled cycles and cycle shares, not seconds. Hardware sampling
can still skid, so function rankings are approximate; scheduler traces remain
the direct source for on-CPU/runnable/blocked time. The short P2/P3 fixture
checks complete work-item boundaries, clocks and zero loss/throttling. No
kernel, tick, affinity or NUMA-policy change is involved.

## Actual task-switch TLB flushes

Retain all 18 hardware P2 profiles from v4 and collect P3 in v5 after validating
all P0/P1/P2 references. Use `reason != 0 || pages != 0` for `tlb:tlb_flush`.
The matching `arch/x86/mm/tlb.c` emits reason 0 with pages -1 when a task switch
actually flushes its context, and pages 0 when an existing ASID is reused
without a flush. Lazy invalidation can defer work until such a switch. Keep
actual switches and exclude no-op switches; counting only nonzero reasons
would miss part of translation invalidation. Full-context events are not
all-process/all-CPU flushes. Receiving-event context alone does not identify
the initiating scanner. Hardware counters remain per CPU, not task-exclusive.

Make this collector refinement only between completed blocks, with an
eight-second fixture confirming complete scanner intervals, loss-free traces,
clock boundaries, the filter and PMU running percentage before P3 begins.
Preserve v4's deliberate boundary interruption and every completed receipt.

## Observed automatic perf adjustment

The completed campaign recorded automatic reductions of the global sampling
ceiling from 63,750 to 20,250/s during hardware readiness and the first P2
baseline. No manual sysctl change was made. All 18 retained P2 recordings
preserve a requested 99-Hz hardware-cycle event and period-one context
tracepoints, below every observed ceiling. Raw record audits found zero loss
or throttling. The final ceiling remains under kernel control; it is recorded
separately from unchanged NUMA/THP/placement/governor settings. Use P0 for
headline runtime overhead, and preserve the adjustment as an instrumentation
limitation. See [completed results](bfs-overhead-profile-results.md).
