# Native CC and BC ARM recovery results — 2026-10-22 UTC

Stock GAPBS CC and BC passed on the original directed Twitter graph, with one and four threads. The running kernel was `7.2.6-cmdp-rearm-production`, behavior commit `0b4d4a2a6174dc5de227252e5d900121b5fd9cb8`. Exercised source/tooling HEAD stayed at `4855bee3cc47220c88926014802b0b0c1bf38b67`. These publication/analysis changes do not change kernel code.

Q=10 s, requested sweep=10 s, poll=1 s; NUMA on, host THP=madvise, target THP off, workers bound individually to CPUs 0–3. Observation includes graph load, initialization, complete trials and cleanup. CC uses weak connectivity on the directed graph; BC uses one sampled source per trial (`-i 1 -l`).

| Workload | Trials | Elapsed s | Algorithm s | ARM admissions | WP | Mean protected GiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cc-1 | 17 | 114.336 | 100.482 | 4157034 | 1 | 4.867 |
| cc-4 | 69 | 120.458 | 106.127 | 4210740 | 1 | 4.719 |
| bc-1 | 2 | 115.834 | 102.328 | 4276948 | 124395 | 5.895 |
| bc-4 | 7 | 111.408 | 98.188 | 4299731 | 1 | 5.949 |

ARM admissions are generations, not unique pages. Mean coverage uses the kernel integral over attachment through final status. All 14 phases passed: 321 fixture checks, 32 generated CC/BC verifications, four full-Twitter correctness gates, four sizing pilots and five observations. Four observations are selected above; the original CC4 56-trial run finished in 99.370 s and is retained. A separate 69-trial CC4 duration refinement finished in 120.458 s, reusing the immutable same-boot audit and prerequisite receipts while rerunning every per-phase preflight and cleanup check.

BC1 recorded 124395 WP events and 125.203 ms total CMD-P helper time (mean 1.006 us, max 11.355 us). The other selected cells recorded one event each. Longer single-thread BC phases could allow state to qualify before later writes, but this is an inference: no page-address or exact phase trace was collected. Different trial counts and scan timing also confound thread comparisons. CC4/BC4 missed 2/3 sweep deadlines; longest completed scans were 13.105/17.618 s. No matched baseline was collected, and helper time is not total fault or application overhead.

The first attempt is preserved: CC4 passed the algorithm verifier but a generic nonzero-admission gate rejected its zero-ARM run. Validation now explicitly permits zero admission for correctness/sizing only; generated exercises and sustained observations remain strict. Five regression checks confirmed that accounting corruption and unsuccessful exits still fail. The final complete matrix restarted after this tooling change.

Final global ARM/publish/revoke counters each equal 91667375; entries/managed/isolated are zero. Boot ID and recorded controls/taint remained stable; no new fault/lockup signature. One ordinary perf message during BC1 verification automatically reduced the kernel sampling limit to 63750. No manual host settings, GRUB or reboot action was performed.

Remote run: `/home/yans3/cmdp-v2/periodic/recovery-ccbc-20261022-v2`; refined CC4 is the sibling `...-v2-refined-cc4`. Driver and archive: sibling `...-v2-driver`. Final raw archive: 461 files, SHA-256 `3659f1d5ba4a08c41a40581590eb85e37a34722d1da407b573c1281838496386`. Preserved first attempt: 184 files, SHA-256 `458ee2dfec8a2cfb3acbc0c6aa3975f2ff3cd37890b3f149beb1c72ccd4c8489`. All were hash-verified locally.

Analysis: `analyze-recovery-native.py BASE --matrix cc-bc --cc4-base REFINED_BASE`, then `plot-recovery-overview.py BASE/recovery-analysis`. The selector records the selected run base in analysis JSON; original evidence is never overwritten. Plots use actual monotonic timestamps and distinguish final cleanup.
