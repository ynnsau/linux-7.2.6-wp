# BFS CPU placement comparison — 2026-10-24

Moving BFS away from CPU 0 reduced median overhead in all four comparisons, but did not guarantee scanner separation. Six of twelve shifted monitored scheduling runs had scanner execution on a BFS CPU. The delayed work can migrate through its non-pinned timer; application affinity does not pin it.

Completed 72 retained jobs: ordinary timing (P0) and separate scheduling traces (P1), each with two placements × one/four threads × off/sampling/dirty→ARM × three repetitions. Shared placement uses CPU 0 or CPUs 0–3; shifted uses CPU 1 or CPUs 1–4. The controller stays on CPU 0. All are separate physical cores on NUMA node 0; no SMT. No kernel or host-policy change.

Twitter BFS has 38/127 fixed trials, 512/512 batch caps and 10-second sweep/quiet intervals. Rotate ABC/BCA/CAB and alternate placement order within adjacent pairs. All jobs use the same child-affinity launcher, warm-cache preparation, RSS observations and complete-job timer. The timer includes graph initialization, complete trials, teardown and STOP/drain.

## End-to-end overhead

P0 medians of three within-repetition B/A or C/A ratios, using the off control at the same placement. Parentheses are the full three-run range, not confidence intervals. Positive reduction is better. Paired reduction is the median of three differences, not the difference of the two medians.

| Threads | Mode | Includes CPU 0 (%) | Excludes CPU 0 (%) | Paired reduction (pp) |
| --- | --- | ---: | ---: | ---: |
| 1 | Sampling | 7.26 (7.02–7.59) | 6.18 (-0.41–6.53) | 1.41 |
| 1 | Dirty → ARM | 8.32 (7.81–13.98) | 6.43 (2.88–8.06) | 5.44 |
| 4 | Sampling | 3.66 (3.48–3.78) | 1.37 (1.22–4.47) | 2.25 |
| 4 | Dirty → ARM | 6.82 (6.34–7.16) | 3.98 (3.62–5.42) | 2.36 |

## Scheduling evidence

Separate P1 traces identify the exact scanner work item, not all work executed by its reused kworker. Values below are medians in seconds. Runnable delay is summed across BFS threads and is not an additive wall-time cost.

| Threads | Mode | BFS runnable delay, shared → shifted | Scanner/BFS same-core overlap, shared → shifted |
| --- | --- | ---: | ---: |
| 1 | B | 8.659 → 5.521 | 8.477 → 5.414 |
| 1 | C | 8.400 → 0.105 | 8.205 → 0.000 |
| 4 | B | 9.128 → 0.283 | 8.759 → 0.000 |
| 4 | C | 12.666 → 1.693 | 12.271 → 1.386 |

The controller, measurement and reporter threads used 0.003–0.068 scheduled seconds on CPU 0 per traced job (median 0.029 s). Moving BFS also removes this work from its cores. Scheduled intervals include interrupt time.

Lower observed scanner overlap accompanies lower runnable delay. This supports direct CPU competition as a contributor, while shared caches, memory bandwidth, TLB effects, synchronous invalidations and placement variability remain. P1 observes its own placement; P0 scanner placement was not traced. Do not assign every saved second to scanner scheduling. Guaranteed isolation needs explicit scanner placement control and uses an extra CPU resource.

## Protection coverage

| Threads | Placement | Mean live ARM (GiB) | ARM / RSS exposure (%) | Peak RSS (GiB) | WP events, three runs |
| --- | --- | ---: | ---: | ---: | --- |
| 1 | shared | 7.81 | 67.3 | 12.32 | 1, 1, 2 |
| 1 | shifted | 8.45 | 72.9 | 12.32 | 2, 2, 1 |
| 4 | shared | 5.44 | 46.8 | 12.32 | 2, 2, 2 |
| 4 | shifted | 5.80 | 50.0 | 12.32 | 1, 2, 1 |

ARM exposure comes from protected byte-time. RSS exposure uses linear interpolation of one-second observations and is approximate; the ratio is time-weighted, not eligible-page coverage. RSS excludes kernel metadata. C−B is a net policy change: ARM also skips later samples.

## Validation and reproduction

All 72 receipts and evidence hashes pass verification; all 36 profiles have zero raw loss/throttling, complete scanner intervals and no parser/scheduler anomalies. Decoded invalidation counters match the controller. Final ownership is zero and there are no new kernel warnings. The existing 26 accounting checks, 17 affinity checks and four revised eight-second readiness fixtures passed. A full shifted four-thread pilot is excluded from measured repetitions. The initial readiness CPU-0-only assertion exposed migration; it was not a kernel failure.

- Experiment source: `a07fe9d39c84f14234b7ae43806c96b9e1248bb2`.
- Kernel: `7.2.6-cmdp-fused-production`, source `fc30294eacbc215b6bc2c0dc11e72f120c5d71d5`.
- Running build ID: `f7e8b245ab7c51144ad8a7445afe80e4b73da8ec`.
- Boot: `04bcabc8-2c68-4e2d-8546-fdcff93a21cd`.
- BMP raw evidence: `/home/yans3/cmdp-v2/periodic/bfs-placement-20261024-v1`.
- BMP analysis: `/home/yans3/cmdp-v2/periodic/bfs-placement-20261024-analysis/analysis`.
- `placement-plan.json` is the expanded execution schedule; inherited `profile-plan.json` is only the recipe.

Analysis commands below read saved evidence without rerunning BFS. Verification and controller analysis use the Python standard library. Plotting was verified with Python 3.9.6 and Matplotlib 3.8.2. Run all commands with the same archive base and output directory:

```sh
mkdir -p analysis
python3 tools/testing/selftests/cmdp/scripts/verify-bfs-placement.py "$BASE" --output analysis/archive-verification.json
python3 tools/testing/selftests/cmdp/scripts/analyze-placement-controller.py "$BASE" --output analysis/controller-scheduling.json
python3 tools/testing/selftests/cmdp/scripts/summarize-bfs-placement.py "$BASE" --output analysis
```

The analysis emits per-run paired results plus four PNG/SVG figures: overhead, runnable delay/overlap, every scanner CPU distribution, and ARM/RSS coverage. Raw runs and plots are archived outside Git.

Next design priority remains low application overhead with substantial ARM coverage. Explicit scanner binding is a future kernel/VM experiment; adaptive revisits and cheaper per-page bookkeeping remain planned.
