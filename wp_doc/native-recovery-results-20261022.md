# Native ARM recovery: BFS and PageRank, 2026-10-22 UTC

The user booted `7.2.6-cmdp-rearm-production`. Fresh provenance checks, all 11 native test phases and four-cell analysis passed. The prior single-thread PageRank late zero-ARM state did not recur. All runs ended with zero entries, managed pages and isolated pages; no new kernel fault or lockup was recorded. No kernel code, GRUB, reboot or manual host-setting changes were made in this round.

## Identity and protocol

- Running GNU build ID: `af98a59cd997005cf396c829f814f03a1986c6b4`.
- Boot ID: `dac56a48-d79a-4db5-b380-575f58fc6c4b`.
- Kernel implementation: `0b4d4a2a6174dc5de227252e5d900121b5fd9cb8`; all native phases froze source HEAD `7846a9473181d1545c95fd2d3a2ba41eeb3b47ca`.
- Image SHA-256: `9de55b107d65a2c5a5457e86f0008faf7f8f8c1ce584485f7a3aea6c2160b323`.
- Original directed Twitter graph: 61,578,415 vertices; 1,468,364,884 edges; SHA-256 `4b7e5193a8e3384b8319359dc8c92534b8aee8e48d6c3bd0820451bb307d6eea`.
- Q=10 s, sweep period=10 s, poll=1 s; NUMA enabled, host THP madvise, target-local THP disabled. Stock GAPBS, pinned singleton workers on CPUs 0–3, OMP dynamic disabled.

Native run: `/home/yans3/cmdp-v2/periodic/recovery-native-20261022-v1`; driver records in its sibling `recovery-native-20261022-v1-driver`. Preparation bound the running ELF notes to validated vmlinux/image and installation receipts, checked the full graph hash, and compiled seven fresh tools. The baseline platform taint 4 and secondary-device boot messages match prior boots; root and graph storage remain `/dev/nvme0n1p2`.

## Acceptance and measurements

The small gate passed 321 TAP checks (legacy 18, monitor 59, dirty 96, recovery 62/86) plus 32 verified generated BFS/PR trials. Full Twitter correctness ran separately: three BFS trials per thread count and one converged PR trial per count, all eight verifications passed. BFS sizing completed 16/36 trials and froze 64/190 observation trials. PR observations used one/four complete trials, each 21 iterations at `-i 1000 -t 1e-4 -l`. Whole trials were completed; actual durations were not forced to 240 seconds.

| Case | Elapsed s | Trials | ARM generations | WP | Invalidations | Mean protected GiB | Peak sampled GiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BFS-1 | 226.16 | 64 | 4,501,619 | 6 | 4,501,609 | 9.462 | 11.858 |
| BFS-4 | 215.30 | 190 | 5,760,565 | 4 | 5,760,561 | 6.809 | 9.874 |
| PR-1 | 249.22 | 1 | 6,951,777 | 13,970 | 6,937,798 | 8.170 | 11.872 |
| PR-4 | 240.91 | 4 | 6,992,133 | 1 | 6,992,130 | 6.924 | 10.389 |

Mean protected GiB is kernel-accounted protected GiB-seconds divided by monitor attachment through final status. It includes startup and cleanup and does not divide by trial compute time. Admissions and invalidations count generations, not distinct pages. No process-wide percentage is inferred.

## Recovery finding and historical comparison

PR1 had no sampled-zero interval after its first positive ARM sample. The historical run had consecutive zeros from 173.236 to 230.284 s (57.048 s), with 113 fully enclosed target CPU-growth intervals. Both runs completed one 21-iteration PR trial. Mean protected memory rose from 2.045 to 8.170 GiB; protected GiB-seconds from 470.931 to 2,027.100. WP increased from 11,583 to 13,970. Thus coverage improved with some additional writes observed; low WP counts alone are not the success criterion.

The only non-startup zero interval in the recovery matrix was BFS4 at 29.036–30.037 s (1.001 s), after an initial tiny positive sample, with one CPU-growth interval. It occurred near startup rather than as a sustained late collapse. Startup zero spans lasted approximately 20/27/30/32 s for BFS1/BFS4/PR1/PR4. Consecutive zero samples cannot exclude brief intervening ARM; CPU progress may be loading, algorithm work or teardown.

Historical mean protected GiB was 7.633/5.569/2.045/2.936 for BFS1/BFS4/PR1/PR4; recovery means were 9.462/6.809/8.170/6.924. Historical BFS used 36/112 trials, recovery 64/190; historical PR4 used two trials, recovery four. These are historical monitored comparisons across boots and unequal work, not matched overhead or speedup measurements.

## Invalidation attribution and remaining costs

| Case | NUMA revoked | Unmap revoked | Clear revoked | Sweeps completed / started | Overruns | Max completed sweep s |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| BFS-1 | 1,857,592 | 2,642,993 | 1,024 | 22 / 22 | 1 | 11.609 |
| BFS-4 | 3,502,906 | 2,257,143 | 512 | 19 / 20 | 2 | 13.172 |
| PR-1 | 4,353,642 | 2,582,577 | 1,579 | 24 / 24 | 1 | 10.143 |
| PR-4 | 4,661,837 | 2,329,085 | 1,208 | 22 / 22 | 2 | 12.627 |

Source-revocation sums equal aggregate invalidations in every cell. Other source buckets revoked zero generations. Attribution identifies the callback path, not necessarily its ultimate cause, and cannot retrospectively attribute older logs. Every diagnostic sample reconciles its 14 exclusive rejection reasons with unknown. Final readonly/saved-dirty rejection totals are zero; resource/report failures are zero. Multi-thread cases retain millions of PROT_NONE sample rejections, a distinct remaining exclusion to study.

Invalidation resets observation/history, so a recovered mapping may contribute another first_arms event. A small rearms counter does not imply recovery failed. The normal zero final state is cleanup; last running samples may already include mapping teardown. The final partial BFS4 sweep was cancelled during exit and is separately reported.

Completed sweeps reached 10.14–13.17 s, exceeding the requested 10-second period. This is a useful next target for cost reduction. WP helper calls matched WP generation counts. PR1 helper mean was about 1.530 microseconds, maximum 2.808 milliseconds, sum 21.380 milliseconds; invalidation helper maxima reached about 1.25–1.63 seconds, dominated by unmap. Helper elapsed times include contention, cover only those helpers, and must not be summed or interpreted as complete architectural fault cost or end-to-end overhead.

All phases preserved the checked NUMA/THP/watchdog/governor/taint/boot settings and zero ownership at boundaries. Ten phase kernel-log deltas were empty; BFS4 observation recorded only the accepted standard perf interrupt-rate reduction notice. No warning, Oops, lockup or new taint was found.

## Artifacts and reproduction

- [Native preparation/run/analysis protocol](../tools/testing/selftests/cmdp/RECOVERY-NATIVE.md).
- [Recovery analyzer](../tools/testing/selftests/cmdp/scripts/analyze-recovery-native.py).
- [Historical comparison](../tools/testing/selftests/cmdp/scripts/compare-recovery-native.py).

The test host lacks matplotlib; analysis ran locally using copied tracked analyzers and checksum-verified raw evidence. Both the detailed 4x4 coverage/event/source/rejection figure and the clearer 2x2 historical/recovery figure are exported as PNG/PDF/SVG. Source snapshots and raw records are preserved separately from derived output.

Raw archive: 372 files, SHA-256 `ab841c5b7057ac6e3a29be2418f199bdc298db43064071e8b8bf3c73a7671c08`; every file was checked against its byte length and hash after transfer. The archive source remains the tested HEAD. Subsequent comparison/report commits change only tools/documentation.

No additional kernel change is required by this test result. Follow-up work can repeat observations to estimate variability, isolate PROT_NONE/NUMA exclusion behavior, and add matched unmonitored/sampling-only runs before claiming overhead.
