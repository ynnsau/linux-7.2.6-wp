# BFS explicit scanner CPU comparison

The user activated `7.2.6-cmdp-cpu-production`. Verify running GNU build ID,
installed artifacts, tested controller, graph and BFS binaries before running.
No kernel build, GRUB update, reboot, native CPU hotplug or global policy change.

Hold BFS at CPU1 (one thread) or CPU1–4 (four threads). Shared selects scanner
CPU1; separate selects scanner CPU0. Controller and RSS observer stay on CPU5;
P1 collectors stay on CPU24/25. Record physical topology and prove placement
using per-session CPU counters in every monitored run and independent P1 traces.
No scanner migration fallback is accepted. A has no scanner and no selector.

Use unchanged Twitter graph, 38/127 whole trials and exact source sequences.
Keep dirty/WP caps 512/512, period/quiet 10 seconds, poll 1 second, per-process
THP disable, node0 placement, NUMA balancing enabled and warm graph before each
timer. A=monitoring off, B=sampling only, C=dirty→ARM. Three repetitions rotate
ABC/BCA/CAB; adjacent shared/separate order alternates. Run 36 P0 measurements
without perf, then 36 separate P1 scheduling captures. Preserve whole-job timing
from before controller Popen through blocking wait return, including graph load,
initialization, all trials, teardown and STOP/drain. Collector setup, warm-cache
read and analysis stay outside this timer.

Exclude readiness pilots: generated graph correctness for both thread counts,
all modes and both placements; full Twitter 1t B/shared and 4t C/separate with
P1. Require matched source sequences, expected worker bindings, active workers,
positive completed sweeps, dirty-ARM admissions, zero controller errors, zero
ownership leaks, clean kernel log and exact CPU policy. Raw traces must have no
loss/throttle, complete scanner intervals, matching worker-entry counts and
balanced scheduler accounting. Keep all failed attempts as diagnostics.

Primary outcome: P0 B/A−1 and C/A−1 within the same thread/placement/repetition.
Report paired placement reductions, all three samples, runtime/RSS and scanner
CPU/runnable/blocked time from P1. Also report live ARM GiB, protected byte-time,
ARM/RSS exposure, WP count/time, flushes and invalidation/lock information.
Prioritize low overhead with substantial ARM coverage; no 95% coverage floor.
P1 runtime is not pooled into P0. Off controls are freshly repeated in both
placement labels despite no scanner. Separate cores still share cache, memory,
IPIs and locks; separation does not eliminate scanner work or isolate every cost.

Entrypoints: `prepare-bfs-scanner-cpu.py --base DIR --expected-source-commit SHA`,
`run-bfs-scanner-cpu.py --base DIR --phase pilot`, then `--phase measured`.
Only successful receipts may be resumed with `--resume`. Do not advance source
HEAD while a campaign is running. Results belong in the study results folder.
