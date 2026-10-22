# ARM recovery regression protocol

This tests whether a private anonymous page can establish a **new** quiet epoch
and regain ARM after an external invalidation removes CMD-P ownership but leaves
its existing read-only PTE untouched. Application stores must not accidentally
repair the deterministic reproducer.

`cmdp_recovery_test` requires root, 4 KiB pages, `CONFIG_CMDP_TEST`, four available
CPUs numbered 0–3 and two NUMA nodes for the explicit migration negative. It runs
a separately attached target with exactly one or four threads, disables THP only
for that target, and controls one initialized 64-page private anonymous range.
The one-thread target is its main thread; the four-thread target is its main
thread plus three workers. Reads and stores use volatile accesses.

The monitor uses Q=200 ms and a 50 ms periodic interval. Deterministic phases use
explicit completed scans, wait a full Q plus 30 ms before admission, and query
ownership, PTE category/flags, PFN and observation epoch under kernel exclusion.
Snapshot contention retries are bounded to two seconds. Each process has a
180-second alarm and the VM has an independent host timeout. The fixture records
actual epoch/admission timestamps; a timeout or missing actual operation is a
failure, never a success or a silent retry of the experiment.

## Before and after

On the diagnostic-only, pre-fix kernel:

```
cmdp_recovery_test --threads 1 --expect-stranded
cmdp_recovery_test --threads 4 --expect-stranded
```

The explicit `--expect-stranded` mode verifies the known bad outcome: all pages
start ARMED; the test hook terminates ownership without changing their exact
PTEs; four full-Q observation passes with application reads only fail to restore
ownership; the original epoch remains invalid; and read-only sampling rejections
increase. Its success marker means **the defect was reproduced**, not that the
kernel implements recovery. Never use this flag for fixed-image acceptance.

On the recovery candidate, the same fixture defaults to strict recovery:

```
cmdp_recovery_test --threads 1
cmdp_recovery_test --threads 4
```

The candidate must prime a new epoch, remain OFF before the fresh Q expires,
then recover every cold page without application writes. Writes to four recovered
pages must terminate their new generations through WRITE and require another
full quiet interval. Additional checks start from ARMED pages and cover genuine
read-only `mprotect` (an actual store must raise SIGSEGV), MAP_FIXED replacement,
MADV_DONTNEED, explicit migration of an unowned recovery epoch, writes during
the recovery epoch, four writers
racing invalidation until an explicit controller stop, STOP during recovery and target exit during recovery.

A separate stage constructs a legitimate saved-dirty read-only PTE using the
ordinary unconditional ARM policy on a dirty target page. After no-op invalidation
and closing that monitor, a fresh qualified monitor must observe the saved dirty
bit as dirty, clear it without adding write permission, and wait a fresh Q.

The fixed fixture also tests the actual kernel PTE predicate using local scalar
values: clean/dirty RW, clean/saved-dirty RO, unsupported RO hardware-dirty,
RW saved-dirty, nonpresent and PROT_NONE encodings. Unsupported encodings must
remain unchanged. These values are never installed into process page tables.

The test-only no-op notification is limited to 1–1024 pages, uses the external
protection notifier path, and changes no PTE. It is a deterministic approximation
of the no-change outcome of a protection walk, not a claim to reproduce every
NUMA scheduler decision. Existing dirty/range suites retain unsupported-page,
swap, legacy interface, scale and lock-wait cancellation coverage.

## Disposable VM suites

Build the initramfs with both stock GAPBS binaries:

```
build-range-vm.sh SOURCE NEW_OUTPUT_DIR BFS_BINARY PR_BINARY
run-range-vm.sh KERNEL INITRAMFS NEW_RUN_DIR recovery-baseline
run-range-vm.sh KERNEL INITRAMFS NEW_RUN_DIR recovery
run-range-vm.sh KERNEL INITRAMFS NEW_RUN_DIR recovery-numa
```

`recovery-baseline` runs only the two explicit expected-stranding cases. `recovery`
runs strict one/four-thread fixtures and verified generated BFS/PR at one/four
threads, eight complete trials per workload. PR uses `-i 1000 -t 1e-4`; no unfinished
iteration is timed out to obtain a convenient duration.

`recovery-numa` runs four actual automatic NUMA cases: one/four threads, local-node
and cross-node reads. It first-touches 128 MiB on node 0, then runs read-only work
for 15 seconds on node 0 or node 1 with periodic observation. The guest-only script
reduces guest NUMA scan intervals. Every case must observe an increase in NUMA
source callbacks **and actual ARM generations revoked by those callbacks**. A
missing callback is a failure, not a pass. After the busy read phase, the first
fixture page must regain a different, fully qualified ARM generation within a
five-second observation bound while the application continues only reading.
The deterministic no-op test additionally proves recovery for every fixture page. Host NUMA settings are never changed.

All existing `small`, `scale`, `numa`, and `dirty` suites remain required. The
collector validates complete nonzero TAP plans, exact workload matrices, eight
correctness verifications per workload, final ownership cleanup, generation
conservation, rejection-reason sums, source accounting, and absence of kernel
warnings. It records baseline reproduction separately from fixed validation.
