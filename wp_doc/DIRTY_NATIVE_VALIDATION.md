# Native dirty-qualified Twitter BFS validation — 2026-10-21

Both native observation runs passed on bmp, with the ten-second dirty qualification policy. The kernel was already booted by the user; this work did not install another kernel, change GRUB, or reboot.

## Provenance and procedure

- Kernel: `7.2.6-cmdp-dirty-production`, build commit `58a253f68da2987845fdefc072c5d2560ad31514`; image SHA-256 `6b72235e0784bc8888d3a3369367249e394def027073598fc72221a0a540fee1`.
- Source during tests: clean `f614f90e6b5482c52f14f9135ac1e04c6bef9541`. Later validation-tool/documentation commits leave the kernel implementation unchanged.
- GAPBS: `941462c7c42e786e4e006a9211b0576ef78d23f7`; stock BFS and directed Twitter graph, 61,578,415 vertices and 1,468,364,884 edges. Header/size checked; full graph hash not repeated.
- Policy: `--dirty-qualify --quiet-ms 10000 --period-ms 10000 --poll-ms 1000 --max-pages 8388608 --max-history 8388608`.
- Threads: 1 or 4; `OMP_DYNAMIC=FALSE OMP_PROC_BIND=close OMP_PLACES=cores`. Per-thread affinity and positive CPU-tick growth confirmed.
- Monitoring spans launch, graph/algorithm initialization, BFS and teardown. Coverage is eligible private-anonymous base pages, with the documented CMD-P exclusions.
- Existing NUMA balancing remained enabled; THP was disabled only for each launched target. Existing 2 GiB swap was used only by the small fixture's own 8 MiB pageout check; no swap or global settings were configured.

Six native tools compiled with GCC 13 and warnings as errors. The completed small suite passed 18 legacy, 59 monitor and 96 dirty checks, followed by 32 generated-BFS verifications across OBSERVE/QUALIFY and one/four threads. Each thread configuration then passed three separate full-Twitter verification trials, an unverified sizing run (16 or 36 trials), and the sustained observation. Sizing selected 36 and 112 observation trials for approximately 130 seconds of BFS computation. All seven final phases passed with balanced ownership and zero final entries/managed/isolated pages, no resource failures, and no kernel fault/lockup signatures.

## Measurements

Dirty-qualified Twitter BFS, one and four threads.

| Metric | 1 thread | 4 threads |
|---|---:|---:|
| BFS trials | 36 | 112 |
| BFS compute (s) | 122.537 | 118.261 |
| Application + monitor elapsed (s) | 133.938 | 130.498 |
| Sweeps started / completed / missed deadlines | 14 / 13 / 0 | 12 / 12 / 1 |
| ARM events (first + re-ARM) | 2,877,539 (2,877,539 + 0) | 4,105,915 (4,105,915 + 0) |
| WP events | 0 | 2 |
| Sampled peak live ARM pages | 2,861,346 | 2,436,211 |
| Last sampled running ARM pages | 0 | 110,770 |
| Kernel-accounted protected integral (GiB-s) | 1017.808 | 721.378 |
| Sampled protected integral (GiB-s) | 1017.727 | 721.694 |
| Dirty / clean observations | 3,745,778 / 6,265,966 | 3,656,862 / 10,569,780 |
| Dirty resets / TLB flushes | 6,141,102 / 6,141,102 | 6,053,948 / 6,053,948 |
| Dirty sampling elapsed sum (s) | 4.509 | 11.762 |
| History payload before controller close (MiB) | 551.32 | 559.98 |
| Write helper calls | 0 | 2 |
| Write helper elapsed sum (ms) | 0.000 | 0.005 |
| Write helper elapsed mean (us) | n/a | 2.271 |
| Workers with observed CPU tick growth | 1 | 4 |

Sweeps, dirty observations and ARM counts can revisit the same page. Last sampled running status precedes the kernel stop request; it is not an exact pre-teardown checkpoint.

Write helper timing includes waits and can overlap across threads. It is neither full architectural page-fault latency nor application overhead. Sampling helper time already includes reset and flush time; do not add these totals. The sampled protected integral uses trapezoidal interpolation of sampled live ARM counts, so it is an estimate rather than exact generation lifetime accounting.

Observation includes graph loading and initialization. There is no matched unmonitored baseline, so this report does not estimate causal overhead or a causal one-to-four-thread speedup.

## Findings and limits

The one-thread run had no write-triggered revocations; the four-thread run had two. Most protection losses were classified as invalidations: 2,877,539 and 4,105,910 generations respectively, including teardown. The aggregate reason does not distinguish every migration, permission, reclaim, or unmap cause. Low WP counts do not mean uninterrupted protection. The four-thread live-ARM curve shows substantial turnover.

Both runs reported zero `rearms`. This is not a unique-address claim: invalidation can reset a page's tracked lifetime, allowing another generation to count as `first_arms`. Total ARM admissions can therefore exceed the maximum simultaneously protected pages.

The sampler's recorded helper time was 4.509 seconds for one thread and 11.762 seconds for four threads. Those totals include 1.609 and 8.632 seconds in synchronous TLB flushing. They exclude outer scanner/eligibility/lock costs and are not measured application overhead. The four-thread run missed one sweep deadline; its longest completed scan was 11.690 seconds. The single-thread final partial scan was canceled normally during target teardown.

Kernel protected byte-time is separately accounted at state transitions with integer rounding. The sampled trapezoidal integral is an independent approximation. A last reported running status may already include memory teardown, so use the full timeline and integral rather than treating it as an application checkpoint.

The kernel logged two standard automatic perf-rate adjustments, to 79,000 during four-thread sizing and 63,000 during its observation. These were recorded; there was no warning stack, Oops, lockup or ownership leak. Boot identity, taint and the configured settings checked by the harness stayed unchanged.

These are functional and coverage measurements, not a matched overhead comparison or causal comparison with previous unconditional ARM runs. A next experiment should hold trial count, placement and policy controls fixed across unmonitored, unconditional ARM, OBSERVE and QUALIFY runs.

## Reproduction and evidence

`tools/testing/selftests/cmdp/scripts/prepare-dirty-native.py --help` documents fresh-run preparation and sequential phase commands. It builds tools and checks the installed-image record without installing or rebooting. `run-dirty-native.py` provides `small`, `twitter-verify`, `twitter-sizing` and `twitter-observe`. Use a new `--base`; after a documentation/test-tool commit, pass its exact `--expected-source-commit` to preparation and each phase. The guard compares the kernel tree against the validated reference while allowing documentation and CMD-P test-tool changes.

Analyze downloaded evidence with `analyze-dirty-native.py RUN_BASE --output OUTPUT_DIR --dirty-row` (Matplotlib required). It emits JSON/Markdown and PNG/PDF/SVG plots with independent WP/ARM scales.

Remote raw evidence: `/home/yans3/cmdp-v2/periodic/dirty-native-bfs-20261021`; local study archive: `results/20261021-dirty-native-bfs`. The 305-file evidence manifest and archive were verified locally. Archive: 609,773 bytes, SHA-256 `cf7278c1441bc61744b8bf157b35425eb9fe30260042158b93c537cb6ccbefa4`.

An initial harness attempt rejected a pre-OpenMP startup affinity sample even though its fixtures and eight BFS verifications passed. It is preserved under `small-initial-affinity-check`. The corrected runtime check requires binding once first observed and for all later matching samples. Another reporting-only issue is preserved: the original small-phase profile listed the Twitter caps; the actual generated-BFS commands used 262,144 active and 524,288 history slots. The published helper corrects that metadata and strengthens the source guard; the archived commands and per-phase harness copies remain the exact executed evidence. The generalized preparation helper was syntax/help-checked after the original preparation ran. No kernel or C test implementation changed in this documentation/tool handoff.
