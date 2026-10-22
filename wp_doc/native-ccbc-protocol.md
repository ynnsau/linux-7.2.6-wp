# Native CC and BC recovery protocol

Extend the existing recovery matrix to stock GAPBS connected components (CC,
Afforest) and approximate betweenness centrality (BC). Keep the original directed
Twitter graph, recovery kernel, Q=10 s, requested period=10 s, poll=1 s, NUMA on,
THP=madvise with target-local THP off, and singleton worker binding to CPUs 0–3.
CC treats directed connectivity as weak connectivity. BC uses `-i 1 -l`, one
sampled source per complete trial with GAPBS's seeded source picker. The graph
is loaded once per process; trial arrays are initialized by the stock program.
Observation includes graph loading, all trials and cleanup.

Preparation audits CC/BC executable hashes along with source, graph and running
kernel provenance. Run with `--matrix cc-bc --target-total-seconds 120`:

1. Fresh preparation and all existing kernel fixtures, then eight verified
   generated CC/BC trials at each of one and four threads (32 application checks).
2. Separate full-Twitter correctness runs, one trial in each of the four cells.
3. Separate two-trial sizing runs in all four cells.
4. Freeze the nearest positive whole trial count to
   `(120 - non_trial_elapsed) / mean_trial_time` from each sizing run. If even one
   complete trial exceeds the requested time, preserve that trial and report it.
5. Observe the four frozen workloads without verifier activity. Require target
   success, exact trial counts, all requested workers active, binding and THP
   controls, accounting conservation, zero final ownership and no new kernel
   warnings, lockups or changes to settings/taint.
6. Analyze with `analyze-recovery-native.py BASE --matrix cc-bc`. Retain raw JSONL,
   commands, source hashes, fixture receipts, sizing decisions and plots.

Each phase runs sequentially and stops the matrix on failure. Do not change
kernel code, GRUB, host configuration or application source. Commit test tooling
before preparation, freeze HEAD throughout the matrix, and publish results only
after evidence validation. No matched unmonitored baseline is part of this
request; elapsed time and write-helper time must not be called total overhead.

Application correctness and timing pilots may legitimately finish without an ARM
admission. Their validation explicitly permits zero admissions while retaining
all sampling, accounting, cleanup and host-health checks. Generated exercises
and sustained observations retain the default nonzero-admission requirement.
The original four-thread CC verification passed GAPBS but exposed this overly
broad harness gate; that attempt remains preserved separately.
