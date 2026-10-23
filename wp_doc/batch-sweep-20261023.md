# BFS batch sweep — 2026-10-23

The design objective is **low end-to-end overhead while retaining substantial ARM coverage**. Coverage is secondary to overhead; there is no 95% coverage-retention requirement. Report absolute ARM GiB, its relation to RSS, WP events and scanner latency so a speedup accompanied by a coverage collapse is visible. This goal applies to future scanner design as well as this campaign.

Use the already running `7.2.6-cmdp-fused-production` kernel. No kernel change, rebuild, bootloader update or reboot is needed. The new `run-fused-batch-sweep.py` inherits the existing launcher, timing boundaries, RSS observer, validators, ownership checks and worker-affinity checks.

Stage one measures equal dirty/ARM ceilings of 64, 512, 4096, 16384 and 65536. Each setting has BFS with one/four threads and three repetitions of A=off, B=sampling-only and C=sampling+dirty-ARM: 90 measured runs. Work remains 38/127 trials, with the same Twitter graph, binary, CPU binding, 10-second period and 10-second quiet interval. Work is not resized to match a new runtime. Mode order is ABC/BCA/CAB; cap order varies across the three blocks. Three repetitions cannot perfectly balance the positions of five caps.

Choose K using the lowest arithmetic mean of the one/four-thread median paired C/A overheads. This is an exploratory choice for the next stage, not an automatic default recommendation. Coverage below 2 GiB mean live ARM or half of the fresh 64/64 value is flagged for interpretation; these flags neither exclude measurements nor create a hard coverage target.

Stage two measures K/K, K/(K/8) and (K/8)/K, including a fresh equal-limit reference: 54 further measured runs. These keep `max(dirty, ARM)` constant, which controls the scanner's maximum address window. All three modes are measured for each pair, including sampling-only. Pair order rotates across blocks; each pair occupies each position once.

One fresh tool preparation and common native gates precede the matrix. New cap pairs receive controller tests, generated BFS verification with both thread counts, full-Twitter C-mode verification with both thread counts, and a sustained four-thread C-mode smoke run before timing. Kernel fault signatures, resource exhaustion, stale ownership, receipt mismatches or target timeouts stop the driver. Maximum actual lock hold and time-budget terminations are recorded; the nominal 20 ms staging budget is not a hard total-hold limit.

The primary outcome is whole-job runtime versus the adjacent same-block off run. Report all repeats, medians, coverage, WP counts/time, RSS, history allocation, unique/shared flush calls and time, actual page occupancy, region skips, lock wait and maximum hold. Different candidate sets and stage-two adaptation must be explicit in the report. Three repeats do not establish precise confidence intervals.

Run `test-fused-batch-sweep.py` first; it checks the 144-run schedule, paired-ratio selection, malformed receipts, asymmetric address windows and inheritance of the existing timing executor without accessing CMD-P. Then use `prepare-fused-overhead.py` with a fresh base and the committed source identity, run the 26 monitor measurement checks, and launch:

```bash
python3 tools/testing/selftests/cmdp/scripts/run-fused-batch-sweep.py \
  --base /path/to/fresh-prepared-sweep --prior /path/to/fresh-prepared-sweep
```

The driver records `stage1-plan.json` before tests, `stage2-selection.json` after all 90 first-stage runs, and `stage2-plan.json` before the second-stage gates/runs. It never revises an existing receipt. `--resume` accepts only successful hashed receipts from the same source and boot. Use separate evidence for any changed source or repeated failed run.
