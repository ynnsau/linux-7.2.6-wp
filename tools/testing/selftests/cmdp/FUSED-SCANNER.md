# Shared dirty/ARM flushes and region scanning

The monitor has two independent, per-session ceilings: `dirty_batch_pages` and
`wp_batch_pages`. Both default to 64; the supported range is 1–65,536 pages.
`CMDP_MON_BATCH_CONFIG` accepts zero as the default, while the CLI requires an
explicit positive value. Configure after ATTACH and before the first scan or
START. Later changes return EBUSY; choose new settings in a new session.

```
cmdp_monitor --dirty-qualify --period-ms 10000 --quiet-ms 10000 \
  --dirty-batch-pages 64 --wp-batch-pages 64 -- COMMAND ARG...
```

## One scanner, one completion boundary

1. Take mmap/VMA write exclusion for one supported writable VMA and an address
   window of at most `max(dirty_batch_pages, wp_batch_pages)` pages.
2. Skip known active runs using the region bitmap. For other pages, obtain a
   no-fault page reference and check eligibility. Cache only the upper PMD table
   location within this chunk; acquire and validate the PTE separately each time.
3. Dirty or unprimed pages join D: clear hardware dirty evidence, invalidate the
   previous observation and hold their folio locks/references. Qualified clean
   pages join A: prepare ownership while retaining the existing final checks.
   D and A are disjoint; a freshly reset page cannot ARM in this sweep.
4. Stop staging at either ceiling, VMA/address-window boundary, STOP/error, or
   the nominal 20 ms staging budget (checked every 64 examined addresses).
5. If A is nonempty, begin the owner-tagged notifier and fast-pin sequence,
   recheck every candidate and change its PTE permissions. Perform **one
   synchronous range flush** for all D and successfully protected A pages.
6. Complete pin checks, end fast-pin/notifier exclusion, then establish D epochs
   from that shared flush-completion timestamp and publish accepted A ownership.
   Drain every pending folio/entry before releasing mmap, including partial
   protection failures and STOP.

A shared flush amortizes the synchronous shootdown across the two sets; it is
not an asynchronous TLB flush. The configured caps are ceilings, not guaranteed
batch occupancy. A 65,536-page window may cause an x86 full-mm invalidation and
may flush substantially fewer changed PTEs when time or the smaller cap ends it.
The staging time target is **not** a hard lock-hold bound: allocation, notifier,
flush, final checks and cleanup can extend the measured hold. `max_hold_ns`
reports the complete observed interval after lock acquisition.

## Compact history and region summaries

The internal history payload is 160 bytes instead of 192: duplicate ioctl
headers/addresses are reconstructed only when reporting. Adjacent histories are
allocated in blocks of 128 (20 KiB, exactly five base pages), with a vmalloc
fallback if a contiguous allocation cannot be obtained. Each 2 MiB region has
presence/ARM bitmaps, four block pointers and a count of active pages. One
XArray lookup reaches the region; adjacent visits reuse it. Blocks and regions
remain alive until worker/notifier/session teardown completes.

Only present, currently ARMED pages can be skipped. A full protected region
skips all 512 histories/PTEs at once; a mixed region skips contiguous active
runs. The bitmap and history state change together under the monitor lock.
Writes clear the relevant bit; mapping/NUMA invalidation and STOP revoke the
associated ownership. Unknown/new/nonresident pages are still examined.

`max_history` bounds both live records and reserved slots (rounded up to one
128-record block). Sparse mappings may reach the allocation bound before the
live-record bound. This is reported as a resource skip; they cannot cause an
unbounded allocation per sparse address. Allocation accounting includes region
slab storage and reserved history pages, excludes XArray/cohort/vmalloc-control
metadata, and must not be confused with target RSS or live history payload.

## Reporting and validation

`CMDP_MON_BATCH_STATUS` / JSON `batch_status` reports both caps; actual total,
shared, dirty-only and WP-only flush calls; flush time counted once; changed-page
counts/maxima; address-span maximum; chunks ending at a cap/time limit; maximum
hold; region/run skips; upper-walk/cache counts; live and allocated history bytes.
`wp_pages` counts PTE protection changes, including changes later aborted;
write-triggered WP events remain `status.terminals.write`.
Legacy dirty/ARM role counters both include a shared flush, so do not add those
role counters to infer actual flush-call count. Other reporting fields and
page-lifetime/generation semantics remain available.

The `fused` VM suite tests one/four CPU writers, all limits from 1 through 65,536,
asymmetric caps, mixed dirty/ARM flushes, fresh epochs, region skips, individual
writes, mprotect/remap replacement, partial ARM abort and STOP while both sets
are pending. Retain all seven prior suites as regressions, with lockdep/debug-VM
and production configurations. Guests have no network or host disk passthrough.
VM correctness and batch occupancy are not native BFS overhead measurements.

After the user activates the installed kernel, native work is BFS only, Twitter
graph, 1/4 threads, fixed 38/127 trials. Use three measured repetitions per
thread/mode: monitoring off, sampling only, and dirty→ARM; rotate mode order.
Keep 10 s period and 10 s quiet interval. Record runtime, RSS, coverage, WP counts
and actual shared-flush diagnostics. First compare 64/64; any additional cap
configuration is another matched three-repetition set, not a replacement for it.

The native helpers are `scripts/prepare-fused-overhead.py` (postboot audit and
fresh tools, no tests) and `scripts/run-fused-overhead.py` (gates plus the 18-run
campaign). They verify the installation/build receipts and inherit the exact
outer runtime/RSS executor. The prepare base can also be the campaign base and
`--prior`; use a new base for each cap pair. Do not use the old 144-run batching
wrapper for this BFS-only three-repeat campaign.

## Design goal and batch-size sweep

Prioritize low end-to-end overhead while keeping substantial ARM coverage.
Coverage is a secondary objective; do not impose a fixed 95% retention target.
Report coverage in GiB and relative to RSS, and flag a large collapse before
recommending a default. This is the user's continuing design goal (2026-10-23).

The two-stage `scripts/run-fused-batch-sweep.py` keeps independent dirty/ARM
controls: first equal limits from 64 to 65536, then three equal/asymmetric pairs
with the same maximum address window. It uses 90 + 54 measured runs, three
repetitions per cell/mode, and the existing timing/validation executor. The
single-pair helper above remains available; the sweep uses a fresh common base
with separate cap/stage/run directories and receipts. See
[the frozen sweep protocol](../../../../wp_doc/batch-sweep-20261023.md).
