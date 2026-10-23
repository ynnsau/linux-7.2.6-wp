# Two-stage BFS batch sweep results — 2026-10-23

**All 144 measured runs and 220 total phases passed.** Equal 512/512 page limits are the best combined BFS candidate in this experiment. Keep the dirty and ARM controls independent; the tested unequal pairs did not improve full dirty→ARM medians. This is a recommendation for subsequent BFS experiments, not a kernel-default change or a claim of a universal optimum.

The continuing design goal is low end-to-end overhead while retaining substantial ARM coverage. Coverage is secondary to overhead; there is no 95% retention requirement.

## Equal-size sweep

Percentages are medians of three within-block whole-job runtime ratios to monitoring off. The same Twitter graph, BFS binary, fixed 38/127 trials, CPU binding, 10-second sampling/quiet intervals and host settings were retained.

| Dirty / ARM pages | Sampling 1t | Sampling 4t | Dirty→ARM 1t | Dirty→ARM 4t | Mean ARM 1t / 4t (GiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64/64 | 7.40% | 4.47% | 11.73% | 7.41% | 6.92 / 5.98 |
| 512/512 | 6.87% | 3.78% | 8.05% | 8.12% | 8.32 / 5.54 |
| 4,096/4,096 | 7.25% | 5.68% | 12.80% | 8.33% | 7.48 / 5.93 |
| 16,384/16,384 | 7.70% | 5.66% | 11.72% | 8.95% | 7.84 / 5.60 |
| 65,536/65,536 | 7.45% | 6.01% | 13.12% | 9.41% | 6.52 / 5.39 |

K=512 was selected by the predeclared average of the one/four-thread median full-mode overheads. Stage one alone favored 64/64 for four-thread full mode (7.41% versus 8.12% at 512/512); 512/512 won the combined objective. Three repeats do not perfectly balance the positions of five sizes.

## Unequal-size comparison

The maximum address window remains 512 pages for all three pairs. The 512/512 reference was measured afresh; stages are reported separately and not pooled.

| Dirty / ARM pages | Sampling 1t | Sampling 4t | Dirty→ARM 1t | Dirty→ARM 4t | Mean ARM 1t / 4t (GiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 512/512 | 6.82% | 4.48% | 8.18% | 7.08% | 8.38 / 5.64 |
| 512/64 | 7.29% | 3.98% | 11.65% | 8.32% | 6.77 / 5.36 |
| 64/512 | 7.97% | 4.56% | 9.74% | 7.57% | 8.21 / 5.66 |

At 512/512, stage-two sampling overhead was 6.82% / 4.48% and full overhead was 8.18% / 7.08% for one/four threads. Full-mode runtimes had medians 130.11 / 129.03 seconds. Mean live ARM remained 8.38 / 5.64 GiB, approximately 68.0% / 45.8% of peak target RSS. Peak target RSS was about 12.32 GiB in all configurations and excludes kernel tracking memory.

One-thread full-mode overhead at 512/512 spanned 6.66–12.21% in stage two; the four-thread range was 6.88–8.28%. Observed repeat ranges overlap other configurations. Selection was adaptive, the sample size is small, and no significance or universal-optimum claim is made. The fresh 64/64 results differ from the preceding campaign; compare within this campaign using its fresh off baselines. Off-run times spanned 120.05–121.52 seconds for one thread and 119.27–120.82 seconds for four.

## What larger batches changed

- Full-mode median unique flush calls at equal 64 → 512 → 65,536 fell from 193,241 → 14,569 → 846 with one thread, and 325,511 → 100,002 → 99,033 with four. Fewer flush calls did not consistently lower end-to-end overhead. These count architecture range-flush requests, not IPIs.
- Maximum measured scanner hold across all modes at those sizes was 10.90 → 10.18 → 34.57 ms. The campaign maximum was 36.35 ms at 16,384. The nominal 20 ms staging target does not bound total completion/lock hold. This is a measured tradeoff, not proof that lock hold alone caused the slowdown.
- At 65,536/65,536, dirty batches reached 65,536 changed pages, but the largest protection batches across full runs were 17,280–18,048 pages. Full-mode median time-budget chunk counts were 775 / 398 for one/four threads. The configured ceiling is not guaranteed occupancy.
- With four threads at 65,536/65,536, median per-run averages were about 183 dirty changes and 78 protection changes per corresponding flush call. Increasing the ceiling therefore did not make most calls large.
- Stage-two 512/512 shared-flush fractions were median 0.0303% / 9.90%; history allocation was about 486.96 / 486.10 MiB. History allocation includes reserved blocks and region headers, excludes other tracking structures, and is separate from application RSS.
- There were 94 write-triggered generation revocations across all 48 measured full-mode runs (0–9 per run). At stage-two 512/512 the counts were 1,3,1 / 1,2,1. These include cleanup and are not all Linux page faults or unique pages.

Coverage is the time integral of live protected bytes divided by whole-job elapsed time, then summarized by the median of three runs. Memory fractions use each run’s peak RSS, not an instantaneous ratio. Component scanner/flush/fault timings are not additive end-to-end overhead.

## Validation and provenance

- 582 native fixture TAP checks, 26 controller measurement checks, nine schedule/comparison checks, 240 generated BFS verifications, 16 full-Twitter verifications and nine sustained smoke runs passed.
- All 144 measured receipts passed source/boot/settings, timing-boundary, source-sequence, trial-count, ownership, batch-cap and hash checks. All 220 total receipts were reverified.
- Final ownership was zero; no new kernel warning/fault signature was recorded. The later host review also passed.
- Running kernel: `7.2.6-cmdp-fused-production`; build ID `f7e8b245ab7c51144ad8a7445afe80e4b73da8ec`.
- Measured source: `b979e609b052dcaf0055840357946ad134fc96ca`; kernel source freeze `fc30294eacbc215b6bc2c0dc11e72f120c5d71d5`.
- Campaign: 2026-10-23 06:09:18–11:53:23 UTC, approximately 5 h 44 min. No kernel, GRUB or boot change was made for this campaign.
- Raw BMP base: `/home/yans3/cmdp-v2/periodic/fused-bfs-batch-sweep-20261023-v1`.
- Local study evidence: `results/20261023-bfs-batch-sweep`; 2,202 copied files, including 2,189 campaign files, passed hash verification.
- Independent reanalysis on Python 3.9.6 and BMP Python 3.6.9 produced identical numeric outputs. Four PNG/SVG/PDF figure families were generated, visually corrected and reviewed.

```bash
python3 tools/testing/selftests/cmdp/scripts/analyze-fused-batch-sweep.py \
  --base /path/to/20261023-bfs-batch-sweep/run \
  --out /path/to/output
```

Use `--no-plots` for numerical verification without NumPy/Matplotlib. Plotting used NumPy 1.26.4 and Matplotlib 3.8.2. The analyzer/report are subsequent source changes; the measured kernel is unchanged.
