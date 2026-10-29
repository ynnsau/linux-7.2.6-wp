# Redis–YCSB baseline results

Maximum observed sustained successful logical YCSB operations/s for this setup. Each configuration has three repetitions, each with 30 seconds of warm-up and 120 measured seconds. No rate cap or latency SLA was imposed.

Redis uses CPU1 and memory node0. The YCSB JVM uses a separate CPU pool on node1. Every run reloads the same saved database. CMD-P sampling and ARM remain inactive.

## Peak throughput

| Workload | Workers / client cores | Median ops/s | Three-run range | Redis CPU | Read / update p99 (µs) | Fewest workers within 3% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 8 / 8 | 74,325.4 | 73,981.3–74,750.4 | 99.8% | 152 / 147 | 8 (8 cores) |
| B | 32 / 8 | 71,836.3 | 71,759.7–72,770.3 | 99.9% | 827 / 821 | 8 (8 cores) |
| C | 8 / 8 | 72,123.1 | 72,038.8–72,124.9 | 99.9% | 154 / — | 8 (8 cores) |

The latency entries are medians of the three per-run p99 values; they are not percentiles pooled across repetitions. Per-run percentiles merge histograms only inside the selected measurement window.

## Memory and validation

- Records: **11,500,000**, with ten 100-byte fields, default hashed keys and the Redis binding’s index.
- Measured Redis allocation across all observations: **15.646–16.332 GiB**; largest Redis RSS: **16.535 GiB**.
- Snapshot restoration: median **73.60 s**, range **71.58–75.61 s**, excluded from throughput. File cache was not dropped.
- Verified **63 runs** and **924 evidence-file hashes**.
- Zero non-OK operations, swaps, evictions, expirations or persistence activity; CMD-P counters unchanged.
- Host policy and thread affinity checks passed. No new kernel crash/OOM/lockup messages were found.
- Golden RDB SHA-256: `67b93f3673f2645b47f17d21dbb6466ae17ad8bd1a2022bf223d9a463fcd2be2`.

## Stability diagnostics at each peak

| Workload | Individual ops/s | Second-half change, each run | Median GC pause fraction | Median longest GC pause |
| --- | --- | --- | ---: | ---: |
| A | 73,981.3, 74,325.4, 74,750.4 | +0.1%, +0.1%, +0.1% | 0.071% | 8.46 ms |
| B | 71,759.7, 71,836.3, 72,770.3 | +0.0%, +0.1%, +0.1% | 0.079% | 9.13 ms |
| C | 72,124.9, 72,123.1, 72,038.8 | +0.3%, +0.2%, +0.4% | 0.074% | 8.61 ms |

Second-half change compares the final 60 seconds against the first 60 seconds of the measured window. It is a diagnostic, not a post-hoc exclusion rule. GC values cover stop-the-world pause overlap with that same window.

## Sweep stopping decision

- A: largest tested concurrency 64; the last two doublings improved the running best median by 0.000% and 0.000% (both below the 3% threshold).
- B: largest tested concurrency 64; the last two doublings improved the running best median by 0.241% and 0.000% (both below the 3% threshold).
- C: largest tested concurrency 64; the last two doublings improved the running best median by 0.000% and 0.000% (both below the 3% threshold).

Client CPU pools tested: 8 cores. Peak Redis CPU and client-pool utilization are recorded above and in the full sweep below.

**8 workers on 8 client cores** are a useful common baseline: every workload is within 0.240% of its observed peak.

B: the common setting has read p99 155 µs versus 827 µs at the observed peak setting (32 workers / 8 cores).

## Full concurrency sweep

| Workload | Client cores | Workers | Median ops/s | Min–max ops/s | Redis CPU | Client pool CPU | Read p99 (µs) | Update p99 (µs) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 8 | 1 | 18,848.9 | 18,767.7–19,034.3 | 55.6% | 6.7% | 90 | 84 |
| A | 8 | 2 | 42,948.5 | 42,781.2–43,544.0 | 76.7% | 14.5% | 73 | 68 |
| A | 8 | 4 | 69,196.8 | 69,077.2–69,209.2 | 99.7% | 23.7% | 88 | 84 |
| A | 8 | 8 | 74,325.4 | 73,981.3–74,750.4 | 99.8% | 29.0% | 152 | 147 |
| A | 8 | 16 | 73,498.7 | 72,981.0–73,778.1 | 99.9% | 29.8% | 362 | 363 |
| A | 8 | 32 | 74,143.9 | 73,748.2–74,235.8 | 99.9% | 30.2% | 804 | 799 |
| A | 8 | 64 | 74,002.8 | 73,596.8–74,598.1 | 99.9% | 30.4% | 1,667 | 1,663 |
| B | 8 | 1 | 17,579.7 | 17,375.6–18,625.7 | 55.4% | 6.6% | 94 | 88 |
| B | 8 | 2 | 42,270.8 | 41,964.2–42,372.2 | 77.0% | 14.5% | 74 | 69 |
| B | 8 | 4 | 67,589.0 | 67,191.4–68,095.8 | 99.7% | 23.6% | 89 | 86 |
| B | 8 | 8 | 71,663.8 | 71,160.4–71,839.4 | 99.8% | 28.4% | 155 | 151 |
| B | 8 | 16 | 71,561.5 | 71,017.7–71,603.3 | 99.8% | 29.7% | 374 | 372 |
| B | 8 | 32 | 71,836.3 | 71,759.7–72,770.3 | 99.9% | 29.8% | 827 | 821 |
| B | 8 | 64 | 71,656.2 | 71,546.0–71,978.3 | 99.9% | 29.8% | 1,719 | 1,716 |
| C | 8 | 1 | 17,257.8 | 17,181.4–17,328.9 | 55.3% | 6.5% | 95 | — |
| C | 8 | 2 | 42,227.0 | 42,027.7–42,603.7 | 77.1% | 14.4% | 74 | — |
| C | 8 | 4 | 67,815.0 | 67,704.4–68,568.8 | 99.7% | 24.0% | 89 | — |
| C | 8 | 8 | 72,123.1 | 72,038.8–72,124.9 | 99.9% | 28.6% | 154 | — |
| C | 8 | 16 | 71,464.7 | 71,078.7–71,606.8 | 99.8% | 30.0% | 375 | — |
| C | 8 | 32 | 72,109.8 | 71,745.7–72,809.6 | 99.9% | 30.0% | 824 | — |
| C | 8 | 64 | 71,555.0 | 71,363.1–73,223.7 | 99.9% | 30.1% | 1,721 | — |

## Interpretation limits

These are observed rates for the pinned versions, data, CPU/NUMA placement and existing host frequency policy. Three repetitions show run-to-run variation but do not establish a universal Redis limit. Logical YCSB operations and Redis command counts differ; the observer also issues a small number of INFO commands. No sampled/ARM comparison was included in this baseline.

New kernel messages are preserved below. The campaign did not start perf profiling. No crash/OOM/lockup diagnostics were found.

```text
[327870.137291] perf: interrupt took too long (6214 > 6212), lowering kernel.perf_event_max_sample_rate to 32000
```
