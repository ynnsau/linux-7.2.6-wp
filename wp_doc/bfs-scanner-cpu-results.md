# BFS fixed-application, explicit-scanner CPU results


**Separating the scanner removes the observed same-core competition and reduces BFS overhead while retaining substantial ARM coverage.** All 72 measured jobs passed: three repetitions per thread/mode/placement in separate P0 timing and P1 scheduling blocks.

BFS stays on CPU1 for one thread and CPU1–4 for four threads. Shared = scanner CPU1; separate = scanner CPU0. Controller/RSS observer CPU5; collectors CPU24/25. Twitter fixed work: 38/127 complete trials, 512/512 dirty/WP batch caps, 10-second sweep/quiet intervals. A = off, B = sampling, C = dirty→ARM.


## P0 matched overhead


P0 has no added profiling. Each B/C runtime is divided by its A control at the same thread count, placement label and repetition. Values are medians with three-run ranges, not confidence intervals. Paired reductions are medians of within-repetition differences; they need not equal the difference between the displayed medians. Bars show medians and dots show every repetition.

Whole-job timing includes launcher, graph loading, initialization, all trials, teardown and STOP/drain. P1 runtimes stay separate. C−B is a net policy difference because ARM also skips later sampling.


| Threads | Mode | Scanner CPU1, shared (%) | Scanner CPU0, separate (%) | Paired reduction (pp) |
| --- | --- | ---: | ---: | ---: |
| 1 | Sampling | 7.41 (7.32–8.10) | 0.62 (0.09–0.69) | 6.79 |
| 1 | Dirty → ARM | 8.31 (8.03–8.70) | 2.70 (2.65–3.32) | 5.61 |
| 4 | Sampling | 4.52 (3.55–4.99) | 1.36 (0.75–1.54) | 3.63 |
| 4 | Dirty → ARM | 7.89 (7.34–8.31) | 3.46 (2.85–3.78) | 4.49 |

Monitoring-off medians, shared → separate: **119.087 → 119.476 s** (1t), **118.164 → 118.535 s** (4t).

## P1 scheduling


All **24 monitored P1 runs and 313 scanner work items** stayed entirely on the requested core. All 12 separate-core traces had **zero scanner execution on BFS CPUs**; the 12 off traces had no scanner work. CPU status also reported zero wrong-core entries/offline events/errors in every P0/P1 monitored job. This resolves the migration ambiguity in the prior affinity-only experiment.

The table contains medians of summed thread-seconds, not additive application wall-time costs. Exact CMD-P work intervals identify scanner activity within reused kworkers. Scheduled time includes interrupts. The controller stayed on CPU5 in all 36 traces, using 0.009–0.076 scheduled seconds per job.


| Threads | Mode | BFS runnable delay, shared → separate (s) | Same-core scanner overlap, shared → separate (s) | Scanner CPU time, shared → separate (s) |
| --- | --- | ---: | ---: | ---: |
| 1 | B | 8.590 → 0.097 | 8.481 → 0.000 | 8.491 → 9.905 |
| 1 | C | 8.978 → 0.079 | 8.859 → 0.000 | 8.889 → 10.739 |
| 4 | B | 9.282 → 0.280 | 8.993 → 0.000 | 9.035 → 10.388 |
| 4 | C | 11.943 → 0.299 | 11.643 → 0.000 | 11.720 → 14.728 |

## ARM/RSS coverage


P0 dirty→ARM values below are medians except the three individual WP counts. ARM exposure is protected byte-time; mean live ARM divides it by whole-job runtime. RSS exposure uses one-second samples and linear interpolation, so the ratio is approximate and is not the fraction of eligible writable memory. Application RSS excludes kernel metadata.


| Threads | Scanner | Mean live ARM (GiB) | ARM exposure (GiB·s) | ARM / RSS exposure (%) | Peak RSS (GiB) | WP events in the three runs |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | CPU1, shared | 8.48 | 1093.9 | 73.2 | 12.32 | 2, 2, 3 |
| 1 | CPU0, separate | 8.23 | 1012.9 | 71.1 | 12.32 | 9, 1, 2 |
| 4 | CPU1, shared | 5.51 | 705.4 | 47.5 | 12.32 | 2, 1, 2 |
| 4 | CPU0, separate | 5.43 | 668.4 | 47.0 | 12.32 | 1, 1, 1 |

## Interpretation


- **CPU competition is a major contributor.** Moving only the scanner removes the measured same-core displacement. The scanner still consumes roughly 10–15 CPU seconds in separate-core profiles; the speedup uses an additional core and does not eliminate that work. Shared caches, memory traffic, TLB effects and synchronous hooks inside BFS remain.
- **Scanner waiting is not application waiting.** In P1 one-thread C, scanner retry sleep rises from 0.879 to 10.038 s while BFS runnable delay falls from 8.978 to 0.079 s. The scanner retries the memory-map write lock after a one-tick sleep. Separate execution permits more concurrent mapping activity; longer scanner waits alone do not demonstrate longer BFS stalls. BFS's own measured mmap/rwsem blocking is 0.389 s (1t) / 0.682 summed thread-seconds (4t) in separate-core C.
- **Lifecycle work deserves the next close look.** P0 separate-core C records median invalidation-callback elapsed totals of 2.879 / 3.847 s (1t/4t), mainly unmap and NUMA paths. These include waiting/preemption and are not tree-only CPU costs. P1 places the measured invalidation callbacks in BFS thread context. Cache-set conflicts specifically remain unproven.
- **Direct measured flush and write-hook time is smaller.** Separate-core P0 C reports 0.025 / 0.209 s inside batch `flush_tlb_mm_range` calls. This excludes later TLB refills and flushes elsewhere. The 12 P0 C jobs record 1–9 WP events and 1.773–22.473 µs total instrumented write-revocation time per job; that is not the full Linux fault-handler time. Do not add these overlapping/scoped counters to infer a wall-time breakdown.

Faster jobs generally complete 12 sweeps versus 13 in shared-core P0; monitor work and runtime therefore feed back into each other. This is the intended fixed-application-work, periodic-monitor comparison, not a constant-scanner-work microbenchmark.

Use explicit separate-core placement as the reference for the next attribution step, then examine unmap/NUMA revocation and metadata access. Adaptive 10→20→40-second revisits remain planned to reduce scanner work. No adaptive policy or further kernel change was implemented in this campaign.


## Validation and evidence


All 72 measured receipts and 14 excluded pilots passed evidence-hash verification. All 36 measured scheduling captures have zero raw loss/throttling, complete scanner intervals, balanced scheduling accounting and no parser anomalies. Traced invalidation counters match independent controller counters. Host checks preserve kernel/boot, NUMA/THP/governor policies; ownership is fully drained and no new BUG/Oops/lockup diagnostics were found.

The kernel automatically reduced its perf rate ceiling to 40,000 during early P0 runs; no manual sysctl change was made. Those messages are separate from raw trace loss/throttling, which remained zero. P0 has no added perf collection.

The SSH/VPN interruption did not stop the durable campaign: it completed on **2026-10-25 at 08:05:07 UTC**. Reconnection and verification occurred October 27 on the same boot; no measured runs were restarted. Excluded pilots comprise 12 generated correctness cases and full Twitter 1t B/shared and 4t C/separate readiness runs.

Kernel `7.2.6-cmdp-cpu-production`; build ID `25320609fc4222e900af5460677651822efcadeb`; boot `2855412a-9e7f-4b5c-96ce-da4d3d98de19`. Kernel implementation [e7f1687a6](https://github.com/ynnsau/linux-7.2.6-wp/commit/e7f1687a654009d2bb478f9d59a655128b4eb74b); experiment harness [5ee0b2c26](https://github.com/ynnsau/linux-7.2.6-wp/commit/5ee0b2c26ae7b0496d0fa6398fa0251520261b30).

BMP run: `/home/yans3/cmdp-v2/periodic/bfs-scanner-cpu-20261025-v1`. The vault archive contains 1,166 hash-verified files, including decoded traces. All 38 raw `perf.data` files (including two pilots) remain on BMP with paths, sizes and SHA-256 recorded in the remote manifest. Analysis/plots are copied to `/home/yans3/cmdp-v2/periodic/bfs-scanner-cpu-20261025-analysis/`.


Plots: `scanner-cpu-overhead.png`, `scanner-cpu-scheduling.png`, `scanner-cpu-scanner-cpus.png`, `scanner-cpu-coverage.png` in the analysis directory. Each has an SVG version. Headline data: `scanner-cpu-summary.json`; diagnostics: `diagnostic-summary.json`.
