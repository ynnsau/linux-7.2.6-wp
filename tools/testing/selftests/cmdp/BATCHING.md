# Bounded dirty-reset and ARM batching

The monitor keeps the same policy, supported pages and UAPI reports. One
serialized worker still scans; period and quiet threshold are unchanged.
Each chunk covers at most 64 addresses in one VMA (at most 256 KiB). It drops
mmap write exclusion and checks cancellation between chunks.

Dirty resets retain only referenced, trylocked folios until one synchronous
`flush_tlb_mm_range`. Each PTE lock is dropped before the next page; no PTE
pointer is retained. mmap/VMA exclusion and folio locks stabilize mappings;
sequence checks discard interrupted observations. A pending reset has no valid
quiet certificate. All reset pages in a chunk start their new epoch at the
shared flush-completion time. Stores during reset-to-flush are outside the
certified interval; stores after completion must be observable by the next
sample. Linux folio dirty accounting and unrelated PTE flags are preserved.

ARM preparation retains existing type, alias, isolation, registry and pin
checks. The monitor additionally requires its sole alias to be exactly the
scanned address. One covering MMU notification surrounds all PTE changes and
the shared flush. Native x86 `ptep_set_wrprotect` atomically preserves hardware
A/D updates and the saved-dirty encoding. All candidates remain ARMING until
flush completion and all final pin/refcount checks. The fast-FOLL_PIN sequence
ends before backend publication or waits. Partial failure and STOP flush any
changed PTEs, then release every staged entry. Abort may leave ordinary RO PTEs,
as before; recovery establishes fresh evidence before another admission.
Legacy explicit multi-alias ARM retains its synchronous per-alias path.

The scanner reuses the probe reference for sampling and ARM preparation, looks
up each VMA once per chunk, skips surviving ARM histories before page lookup
under VMA fault exclusion, and aggregates scan-only counters once per chunk.
Ownership counters still update at the actual admission/terminal transitions.

## Reporting

All existing fields and layouts are preserved. `dirty_status.flushes` retains
its page unit: pages covered by completed dirty-reset flushes, equal to resets.
`flush_ns` counts actual shared flush time once; `sample_ns` includes it once.
New debugfs stats distinguish actual calls from pages:

- `dirty_flush_batches`, `dirty_flush_pages`;
- `arm_flush_batches`, `arm_flush_pages`;
- `scan_active_skips`, `scan_page_lookups`.

These are global cumulative counters; take differences around an isolated run.
They are not IPI counts. x86 chooses range versus broader TLB invalidation using
its normal architecture policy. `arm_ns` remains summed entry lifetime during
arming; batched entries overlap, so it is not CPU time or end-to-end overhead.
No overhead reduction is claimed until matched native tests after user boot.

## Disposable VM acceptance

Run `scripts/run-range-vm.sh KERNEL INITRD NEW_DIR batch` plus the existing dirty,
small, scale, numa, recovery and recovery-numa suites on debug and production
images. The host wrappers require zero host ownership, enforce watchdogs, check
host identity/counters and reject guest warnings. No network, shared filesystem
or host block device is passed to the guest.

`cmdp_batch_test` covers 1/63/64/65/129/513 pages at one and four threads, actual
flush call/page counts, common post-flush epoch boundaries, survivor skips,
WP/requalification, a seven-page active limit, four writers during a deliberately
exposed reset window, ARM publication ordering, partial protection abort and
STOP during a pending ARM batch. Existing suites cover swap, permissions,
remapping, pins, aliases, NUMA recovery, churn, exit and generated BFS/PR.
The additive CONFIG_CMDP_TEST ioctl is privileged, one-shot and bounded to
1000 ms. It is inactive unless explicitly configured. `test_batch_waiting`
exposes the injected phase; it is zero outside the pause.

Build/install uses a new versioned release. Do not call `make install`,
`update-initramfs`, GRUB update commands, or reboot. Install modules and versioned
boot files directly, generate the initramfs with `mkinitramfs`, and verify boot
configuration hashes remain unchanged. The user chooses GRUB and reboots.
