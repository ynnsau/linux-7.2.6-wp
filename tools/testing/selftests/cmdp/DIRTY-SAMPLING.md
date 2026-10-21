# Hardware dirty sampling and quiet-qualified ARM

The optional policy samples eligible ordinary writable anonymous 4 KiB PTEs.
`--dirty-observe` performs observation without ARM; `--dirty-qualify` admits a
page only after the configured quiet interval. The default controller behavior
remains unconditional ARM. No application source change or readiness signal is
required. The existing exclusions, including THP, aliases, pinned pages and
swapcache, still apply.

```
cmdp_monitor --dirty-observe --period-ms 10000 --quiet-ms 10000 -- COMMAND
cmdp_monitor --dirty-qualify --period-ms 10000 --quiet-ms 10000 -- COMMAND
```

Configure `CMDP_MON_DIRTY_CONFIG` after ATTACH and before the first scan or START.
Modes are DISABLED, OBSERVE and QUALIFY; the minimum nonzero quiet threshold is
10 ms and zero selects 10000 ms. Inventory and dirty modes are mutually
exclusive. A dirty observation means at least one write, not the number of
writes. Initial observations, WP, mapping changes and lost continuity require
a new synchronized dirty-reset/flush boundary. Existing ARM survivors stay
protected. Quiet duration is elapsed monotonic time from that boundary, not a
count of nominal sweep periods.

The controller adds `dirty_status` JSONL records with mode, threshold, issued
and completed sampling passes, dirty/clean samples, priming, unknown windows,
qualification, late-dirty rejection, reset/flush counts and time, sampling time,
invalidation/WP resets, resource skips, current valid observations and metadata
bytes. Existing status, progress and cohort records remain unchanged.
`qualified` counts observation epochs satisfying Q, including OBSERVE mode;
it is not an admission count. Use existing first_arms/rearms for actual ARM.
Per-page `CMDP_MON_DIRTY_PAGE` exposes the epoch, reset completion time and
qualification time for small fixture diagnostics. STOP makes observations
invalid while retaining their diagnostic timestamps and counters.

`validate-run.py` checks the additive counter relationships, monotonic
cumulative values, STOP cleanup, no ARM/write revocation in OBSERVE, and actual
nonzero admissions in QUALIFY. It rejects a qualification experiment that ends
before admitting any page. A handler-call count can include calls unrelated to
a newly owned page, so observe-mode checks use ARM and CMD-P WRITE terminals.

## Disposable VM validation

Build an initramfs with `scripts/build-range-vm.sh`, then run
`scripts/run-range-vm.sh KERNEL INITRAMFS NEW_RUN_DIR dirty`. The host wrapper
creates a fresh 256 MiB regular file in the run directory and exposes that file
as `/dev/vda`; it never exposes a host block device. Guest tests format this
disposable disk for swap. The wrapper checks host boot identity and CMD-P
counters before and after. The destructive guest setup additionally requires
both exact `cmdp_suite=dirty` and `cmdp_disposable_swap=1` kernel command-line
markers and the expected disk size before formatting `/dev/vda`. The wrapper
enforces a timeout and rejects kernel warnings.
The guest needs four CPUs, the existing two-NUMA-node topology,
CONFIG_CMDP_TEST, CONFIG_SWAP, CONFIG_PROC_PAGE_MONITOR,
CONFIG_CHECKPOINT_RESTORE, CONFIG_MEM_SOFT_DIRTY and built-in
CONFIG_VIRTIO_BLK. It has no network or shared filesystem.

The dirty suite runs the legacy page, monitor and bounded range/cancellation
tests, then the new `cmdp_dirty_test` TAP test. The fixture covers:

- Invalid policy configuration and changes after the first scan.
- First-visit priming, actual quiet-time qualification, interval writes, hot
  pages remaining OFF, WP followed by fresh qualification, and ARM survivors.
- Sampling-only zero ARM and zero CMD-P write revocations.
- OFF history invalidation by mprotect, MAP_FIXED reuse and explicit NUMA
  migration; the migration must change PFN and observation epoch.
- Four writers on CPUs 0–3 racing dirty resets/TLB flushes, with simultaneous
  mprotect and MADV_DONTNEED churn and complete data checks.
- Preservation of PTE soft-dirty and Linux KPF_DIRTY after hardware dirty
  harvesting, proven through pagemap and kpageflags.
- Actual swapout of an 8 MiB region after dirty harvesting, proven with pagemap
  swap entries, and full pattern verification after swap-in. Swapcache can
  remain outside the supported scope; it must not inherit old quiet evidence.
- Bounded history exhaustion and STOP cancelling a sampler while another
  thread demonstrably holds the target mmap read lock.

Finally, generated GAPBS BFS (`-g 18 -n 8 -v`) runs at one and four threads in
OBSERVE and QUALIFY. All eight verifications must pass in each run, valid
samples must be observed, and QUALIFY must actually ARM pages. JSONL and final
cohorts are retained in the serial log; redundant repeated cohort snapshots
are omitted to keep guest-console output bounded.

These tests validate mechanism and data preservation in a small guest. They
do not establish native Twitter-scale overhead, quantify missed writes from
unsupported writers/mappings, or implement adaptive retry thresholds. The
20-scan concurrent stress test increases race exposure but is not proof that
every possible interleaving occurred. The initial kernel policy remains a fixed
quiet threshold; adaptive admission is a later experiment.
