# Paired BFS CPU placement

Test whether direct sharing of CPU 0 with the scanner explains the observed
slowdown. Keep the controller on CPU 0 in both placements. Set affinity in its
forked child before the direct BFS exec, using the same new `--target-cpus`
option in every case. A taskset executable inside the target command would
introduce a second exec after attachment; avoid that lifecycle ambiguity.

Shared placement: one-thread BFS on CPU 0, four-thread BFS on CPUs 0–3.
Shifted placement: one-thread BFS on CPU 1, four-thread BFS on CPUs 1–4.
All five CPUs are separate physical cores on NUMA node 0, with no SMT.
Do not change worker affinity, workqueue policy, kernel, NUMA or THP settings.
The per-CPU delayed work is initially queued by the controller and subsequently
requeues itself using a non-pinned global timer. The short readiness fixture
showed migration when CPU 0 became idle. Verify actual scanner execution with
workqueue and scheduler traces; flag migration and report on-application-core
time and same-core runnable overlap. Do not call a shifted run fully separated
when the scanner migrates onto a BFS CPU. No shared-worker pinning is introduced.

Two separate blocks: P0 ordinary runtime and P1 scheduling attribution.
Each has 2 placements × 2 thread counts × 3 modes × 3 repetitions = 36 runs.
Use A monitoring off, B dirty sampling only and C dirty→ARM. Rotate mode order
ABC/BCA/CAB; alternate which placement goes first within adjacent matched
pairs. All 72 runs use Twitter, 38/127 fixed BFS trials, 512/512 batch caps,
10-second sweep/quiet intervals, the same warm-cache preparation and 1-second
RSS observer. Bind OpenMP workers to the corresponding explicit CPU places.
Verify actual worker masks and active threads from `/proc` observations.

Reuse the unchanged complete-job timing executor: launcher, graph load,
initialization, whole trials, teardown and monitoring STOP/drain. Do not resize
work after seeing new runtimes. Keep P1 profiling times out of P0 overhead.
P1 uses the prior validated scheduler/workqueue/write-mmap/notifier collectors
on CPUs 24–25, native lifecycle clock boundaries and raw loss/throttle audits.
Short hot/cold fixtures precede measured runs to verify the collector and
child/controller separation. Existing monitor accounting tests and explicit
affinity parsing/effective-mask tests must pass before campaign launch.

`profile-plan.json` is the inherited single-placement recipe;
`placement-plan.json` expands its schedule and is the execution authority.
Keep source/binary/kernel/boot/work fingerprints, three identical source
sequences per cell, guard host settings and ownership cleanup after every run.
All launcher changes are userspace; no kernel build/install/reboot is needed.

Report paired B/A and C/A overhead within each placement and repetition,
then the paired change in excess seconds and percentage points between
placements. Include every repeat, ARM exposure/GiB, RSS, WP events, scanner
CPU time, BFS runnable delay and same-core overlap. Controller interference
also moves off BFS when the application is shifted; measure its scheduled
CPU where possible and avoid claiming scanner-only causality from placement
alone. Shared LLC/memory bandwidth, TLB shootdowns, synchronous callbacks and
CPU-frequency/background variability remain. Separation uses an extra core
resource; it does not eliminate scanner CPU consumption.
