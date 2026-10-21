# Native dirty-qualified Twitter PageRank

This protocol extends the native dirty-qualified BFS experiment to stock GAPBS
PageRank, with one and four OpenMP threads and a target of approximately 120
seconds of total application/controller elapsed time per sustained observation.
The kernel is `7.2.6-cmdp-dirty-production`; this protocol uses its existing
implementation and the existing GAPBS `pr` executable.

Both thread configurations passed correctness and protection-accounting
checks. **Four-thread observation took 121.506 seconds; one-thread observation
took 231.256 seconds and missed the 120-second target.** One full converged
single-thread trial alone required 222.336 seconds. The run completed naturally
with the normal convergence settings.

Recorded inputs:

| Item | Identity |
| --- | --- |
| Kernel source at experiment start | `b7c8523ec28de955a6e7e45c0e2f6b100fa5ddd3` |
| Validated kernel reference | `f614f90e6b5482c52f14f9135ac1e04c6bef9541` |
| GAPBS commit | `941462c7c42e786e4e006a9211b0576ef78d23f7` |
| PR binary SHA-256 | `306446a86af9b50f936d66ba456d3c3c2141df818810400c7c566de7938b401b` |
| Graph | `benchmark/graphs/twitter.sg`: directed, 61,578,415 vertices, 1,468,364,884 edges, 12,732,173,745 bytes |
| Recorded run root | `/home/yans3/cmdp-v2/periodic/dirty-native-pr-20261021` |

**Workload and observation.** Each PR trial uses `-i1000 -t1e-4 -l`, preserving
the convergence settings in GAPBS `benchmark/bench.mk` while logging iterations.
The 1,000-iteration value is a maximum; the algorithm stops earlier when its
iterate-change error falls below the tolerance. `-n` selects independent trials.
Graph loading occurs once per process; each trial initializes its algorithm
state again. The controller observes from process launch through natural exit
and STOP, including graph loading, initialization and teardown.

The Twitter policy is `--dirty-qualify --quiet-ms 10000 --period-ms 10000
--poll-ms 1000 --max-pages 8388608 --max-history 8388608`. Eligible coverage is
private anonymous 4 KiB pages, with the implementation's existing mapping/page
exclusions. The active-page limit represents 32 GiB; the history payload limit
is 1.5 GiB before other metadata overhead. Host settings remain as found.
The controller disables THP for its child; the harness requires host THP
`madvise` and automatic NUMA balancing enabled.

**Preparation and reuse.** The archived `prepare-dirty-pr.py` is specific to the
recorded paths and commits. It audits the running kernel and installed-file
hashes, PR identity, source status, and the earlier BFS fixture results. It
reuses six hash-verified native tools from
`/home/yans3/cmdp-v2/periodic/dirty-native-bfs-20261021/tools` and the passing
18 legacy, 59 monitor and 96 dirty checks from the same boot. It writes
`postboot-audit.json`, `reused-fixture-summary.json`, `reused-tools-build.json`
and `kernel-boot.log`; these are required inputs for the PR harness.

The preparation helper has no generic CLI and refuses to replace an existing
audit. The phase harness has path options but does not prepare a new machine.
For a future run, use a fresh directory, rerun the native fixtures after any
reboot, and adapt preparation to the new paths, audited source commit and
same-boot fixture receipt. Copy receipt bytes exactly: their SHA-256 values
must match the audit. A new boot cannot reuse the recorded fixture pass as its
readiness evidence. Do not relabel a BFS audit as a PR audit.

**Phase commands.** Five phases completed: `small`, `twitter-verify-t1`,
`twitter-verify-t4`, `twitter-observe-t1` and `twitter-observe-t4`. Both
observations used `--sizing-from-verification`; no separate `twitter-sizing`
phase was run. These commands describe the recorded setup after successful
preparation. A phase creates a new output directory and refuses to reuse it.
For same-boot recovery, inspect existing `summary.json` and `exit` records and
continue only with missing phases whose predecessors passed; preserve failed
attempts. Do not run multiple phase controllers concurrently.

```sh
cmdp_run=/home/yans3/cmdp-v2/periodic/dirty-native-pr-20261021
cmdp_harness="$cmdp_run/run-dirty-pr.py"
cmdp_head=b7c8523ec28de955a6e7e45c0e2f6b100fa5ddd3

sudo python3 "$cmdp_harness" small --base "$cmdp_run" \
  --expected-source-commit "$cmdp_head"

# Run and inspect each phase separately.
sudo python3 "$cmdp_harness" twitter-verify --base "$cmdp_run" \
  --threads 1 --expected-source-commit "$cmdp_head"
sudo python3 "$cmdp_harness" twitter-verify --base "$cmdp_run" \
  --threads 4 --expected-source-commit "$cmdp_head"
sudo python3 "$cmdp_harness" twitter-observe --base "$cmdp_run" \
  --threads 1 --target-seconds 120 --sizing-from-verification \
  --expected-source-commit "$cmdp_head"
sudo python3 "$cmdp_harness" twitter-observe --base "$cmdp_run" \
  --threads 4 --target-seconds 120 --sizing-from-verification \
  --expected-source-commit "$cmdp_head"
```

`small` runs eight verified generated-graph PR trials for each combination of
OBSERVE/QUALIFY and one/four threads: 32 verification passes are required.
It uses `-g18`, a 100 ms sweep period and 10 ms quiet qualification.
Twitter verification uses one verified converged trial per thread count;
the optional independent `twitter-sizing` phase uses one unverified trial.
PR verification performs a serial graph pass, so its time must not enter the
observation sizing estimate.

Before observation, inspect the pilot. The automatic selection is the nearest
positive integer to `(120 - nontrial_seconds) / mean_trial_seconds`, where the
pilot's nontrial time is its elapsed time minus measured trial time. The
`sizing-selection.json` record captures the estimate and selected trial count.
Whole trials may prevent a close two-minute fit, especially if one trial
already exceeds the target. `--trials` records an explicit override; do not
change tolerance, insert waits, or terminate a trial to force the duration.

If a verified trial already exceeds the 120-second target, avoid another
equally long sizing-only run by invoking `twitter-observe` with
`--sizing-from-verification`. This explicit option uses the passing same-boot
verification pilot for that thread count, subtracts every reported
`Verification Time` from elapsed time, and derives the nontrial estimate as
`elapsed - trial_seconds - verification_seconds`. It requires matching,
positive finite timing records and passing verification for every trial.
The sizing record identifies the source phase, hashes its summary, command
and target log, and records how much verifier time was removed. No
`twitter-sizing` directory is created or implied. The default remains an
independent unverified sizing run; the shortcut cannot be combined with an
explicit `--trials` override. Verifier-related interference can still affect
the pilot estimate, so record the actual unverified observation duration.

**Validation and interpretation.** `OMP_DYNAMIC=FALSE`, `OMP_PROC_BIND=close`
and `OMP_PLACES=cores` accompany `OMP_NUM_THREADS=1` or `4`. `/proc` sampling
checks the requested thread count, binding, CPU-tick activity and disabled
target THP. Require successful verification output, successful child/controller
exit and STOP, nonzero QUALIFY admissions, balanced cohort/session accounting,
zero resource failures or dropped reports, zero final ownership, unchanged
boot/settings/taint, and kernel-log continuity without new fault signatures.
The existing strict admission check also applies to short Twitter pilots.

Preserve `monitor.jsonl`, target and kernel logs, thread samples, exact
command/environment, provenance, validation reports, and phase summaries.
Report elapsed time separately from summed `Trial Time`; report per-trial
iteration counts and rounded logged change separately from verification
results. Retain sweep counts/overruns, ARM admissions, WRITE terminals, live
coverage, protected byte-time, dirty observations/resets/flushes, history
payload and helper timings.

ARM admissions can revisit pages; invalidations can end coverage without a
write. Write-helper timing includes waits and is not full page-fault latency.
Sampling time includes reset/flush work and is not application overhead.
The asynchronous PR update order can yield different iteration counts across
thread counts. These duration-targeted runs need matched fixed-work baselines
before supporting overhead or speedup claims.

**Measured results, 2026-10-21.** All five phases passed on boot
`a27a890b-9239-4f42-864a-1a0561b1f242`. There were 32 generated-graph verification
passes plus one full Twitter verification pass at each thread count. The
existing 173 native fixture checks were reused from this same boot. Actual
one/four-thread execution and CPU activity were observed. No kernel or
application source change, kernel installation, bootloader update, host
configuration change by the operator or reboot was required for these tests.
During single-thread verification, the kernel made its routine automatic perf
sampling-rate reduction to 50,250; this was recorded in the kernel log.

| Sustained observation | 1 thread | 4 threads |
| --- | ---: | ---: |
| Independent PR trials | 1 | 2 |
| Iterations per trial | 21 | 21, 21 |
| PR trial computation (s) | 222.336 | 113.036 |
| Application + monitor elapsed (s) | 231.256 | 121.506 |
| Difference from 120-second target (s) | +111.256 | +1.506 |
| Sweeps started / completed / missed deadlines | 24 / 23 / 0 | 13 / 12 / 0 |
| ARM admissions, first + re-ARM | 3,119,029 | 2,980,093 |
| Re-ARM admissions included above | 1,877 | 0 |
| WRITE terminals / WP events | 11,583 | 0 |
| Sampled peak live ARM pages | 2,560,576 | 1,406,671 |
| Kernel-accounted protected integral (GiB-s) | 470.931 | 355.556 |
| Dirty sampling elapsed sum (s) | 16.155 | 12.673 |
| Write-helper elapsed sum (ms) | 13.611 | 0 |

Each observation canceled one final partial sweep normally at process exit.
Both completed with zero resource failures, dropped reports, dirty resource
skips or remaining owned pages, and no new kernel fault signatures. The
four-thread generated OBSERVE and QUALIFY checks each had two missed 100 ms
deadlines; the ten-second Twitter observations had none.

The verification pilots measured 222.719 and 58.747 seconds of PR computation.
Sizing removed 16.374 and 16.264 seconds of serial verification, estimating
231.438 seconds for one single-thread trial and 127.312 seconds for two
four-thread trials. The one-thread duration miss was known before observation;
one trial was the smallest positive amount of complete work.

One-thread live protection had fallen to zero by the 173.236-second status
sample and stayed zero through the last running sample at 230.284 seconds,
while PR was still computing. Its 3,107,446 invalidation terminals dominate
the lifetime accounting; the aggregate reason does not identify which
migration, reclaim, permission or unmap path caused the loss. Zero WP counts
therefore cannot establish sustained protection. Four-thread accounting
ended 2,980,092 generations through invalidation and one through control STOP.

For a comparable monitoring prefix, the last status samples before 120 seconds
were at 119.157/119.158 seconds. They recorded 3,114,652/2,980,092 cumulative
ARM admissions and 7,209/0 WP events, with 524,289/625,918 live ARM pages. These
are prefix observations, not completed equal-work PageRank results or overhead
measurements. Full raw records and derived figures remain in the dated run
archive; the repository contains the harness, analyzer and this report.
