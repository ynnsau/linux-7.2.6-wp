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
