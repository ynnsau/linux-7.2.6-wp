# Shared-flush BFS overhead — 2026-10-23

All 40 phases passed on `7.2.6-cmdp-fused-production`: 582 fixture TAP checks, 48 generated BFS verifications, four full-Twitter verifications, three sustained smoke runs and 18 measured runs. The controller measurement checks also passed (26 checks).

The measured campaign uses the same Twitter graph and BFS binary as the preceding batching campaign, with 38 trials for one thread and 127 for four threads. Three repetitions per mode use ABC / BCA / CAB order, where A is off, B is sampling-only and C is sampling + dirty→ARM. Period and quiet interval are both 10 seconds; dirty/WP caps are 64/64. Target CPU binding, NUMA balancing, THP policy and governors match the prior campaign.

## End-to-end overhead

Percentages are medians of within-block ratios to that block’s off run, including launch, graph loading, initialization, whole trials, cleanup and STOP/drain. Previous results have six repetitions; current results have three.

| Threads | Previous sampling | Current sampling | Previous dirty→ARM | Current dirty→ARM |
| --- | ---: | ---: | ---: | ---: |
| 1 | 11.81% | 7.04% | 11.83% | 9.54% |
| 4 | 6.88% | 4.08% | 10.54% | 8.61% |

| Threads | Off median (s) | Sampling median (s) | Dirty→ARM median (s) | Off baseline drift |
| --- | ---: | ---: | ---: | ---: |
| 1 | 120.25 | 128.97 | 131.74 | +0.29% |
| 4 | 119.35 | 124.41 | 129.83 | -0.22% |

The observed overhead decreased. This is a comparison across two boots of the combined changes, with different repeat counts and mode orders; it does not isolate each optimization or establish a confidence interval.

## Coverage and scanner diagnostics

| Threads | Previous mean live ARM (GiB) | Current mean live ARM (GiB) | WP events in the three current runs | Shared flush calls, median | Shared fraction, median |
| --- | ---: | ---: | --- | ---: | ---: |
| 1 | 8.562 | 8.510 | 2, 2, 3 | 7 | 0.0051% |
| 4 | 5.689 | 5.746 | 1, 1, 1 | 45,685 | 13.8733% |

Mean live ARM is time-averaged across the whole job and then summarized by the median of runs. Coverage stayed close to the previous campaign (−0.62% / +0.99% for one/four threads). Target peak RSS was approximately 12.32 GiB in every mode; it excludes kernel tracking memory. WP counters count write-triggered generation revocations, including cleanup activity, rather than unique pages or every Linux fault.

Shared coalescing was material with four threads and rare with one. Full-mode unique flush-call medians were 136,923 / 317,440, versus 112,412.5 / 331,122.5 before; lower runtime cannot be explained solely by fewer calls. Timed dirty sampling in sampling-only mode fell from 6.50 to 4.60 seconds with one thread and from 5.20 to 4.14 seconds with four threads. These component timings are not additive end-to-end overhead.

Full-mode region skips covered median 27.98 million / 16.71 million page visits, including 54,630 / 29,730 whole 2 MiB skips. These are repeated visits, not unique pages. PMD cache-hit fractions were 98.2–99.1% across the mode/thread medians. History payload is 160 bytes per live record; measured full-mode allocation was approximately 487.2 / 486.1 MiB. Allocation includes reserved history blocks and region headers and is distinct from payload and target RSS.

All observed dirty/protection batch maxima stayed within 64 pages, and the largest measured scanner hold across the campaign was 9.354 ms. No resource-skip or ownership leak was reported. No kernel warning/fault signatures appeared during the campaign.

## Provenance and reproduction

- Measured source commit: `24b8aa263150c1ec721cfd0205f99956274813c4`.
- Kernel source freeze: `fc30294eacbc215b6bc2c0dc11e72f120c5d71d5`.
- Running build ID: `f7e8b245ab7c51144ad8a7445afe80e4b73da8ec`.
- Boot ID: `04bcabc8-2c68-4e2d-8546-fdcff93a21cd`.
- Raw BMP evidence: `/home/yans3/cmdp-v2/periodic/fused-bfs-64-64-v1`.
- Local study evidence: `results/20261023-fused-bfs-overhead` under CMD-P write protect study.
- All 428 copied files and the 418-entry raw manifest passed hash verification.
- Final ownership was zero; running kernel, boot ID, settings and kernel sources were unchanged throughout measurement.
- No kernel rebuild, GRUB changes or reboot were performed in this campaign.

The analysis checks all 18 current receipts and the 36 prior BFS receipts, matched source sequences, frozen work, hashes, exact timing boundaries, batch limits and host guards. It emits JSON/CSV and PNG/SVG/PDF figures. The runtime/RSS plot layout was visually corrected; all final PNGs were reviewed.

```bash
python3 tools/testing/selftests/cmdp/scripts/analyze-fused-bfs.py \
  --base /path/to/20261023-fused-bfs-overhead/run \
  --previous-root /path/to/20261022-batched-gapbs-overhead \
  --out /path/to/output
```

Analysis environment: Python 3.9.6, NumPy 1.26.4, Matplotlib 3.8.2. The report and analysis are subsequent documentation/tool changes; the measured kernel code is unchanged.
