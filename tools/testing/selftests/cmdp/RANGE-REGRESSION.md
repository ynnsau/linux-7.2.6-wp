# CMD-P range invalidation and cancellation regression

These tests address the October 21 Twitter pilot failure: a notifier callback
repeatedly searched a multi-million-entry list while a scanner and STOP waited
behind the target's mmap read lock. Run the new suites in a disposable virtual
machine before attempting another large native workload.

The guest kernel uses the candidate source. The host keeps its running kernel,
modules, boot files, and GRUB configuration. There is no host CMD-P session,
network interface, guest disk, shared filesystem, or device passthrough. A host
watchdog terminates the QEMU process if the guest hangs. This contains an ordinary
guest kernel panic or deadlock; it is not a guarantee against defects in QEMU,
KVM, or the host kernel itself.

## Build and run

Build an x86-64 kernel with CMDP, CMDP_TEST, DEBUG_FS, PROVE_LOCKING,
DEBUG_ATOMIC_SLEEP, DEBUG_VM, NUMA, NUMA_BALANCING, KPROBES, KPROBE_EVENTS,
FTRACE, DEVTMPFS, BLK_DEV_INITRD, RD_GZIP, and SERIAL_8250_CONSOLE enabled.
CMDP_TEST is needed only for the bounded mmap reader contention fixture.
Do not install this debug kernel on the host.

On the Linux build host, with its static BusyBox and a working GAPBS BFS binary:

```sh
scripts/build-range-vm.sh SOURCE NEW_IMAGE_DIR GAPBS_BFS_BINARY
scripts/run-range-vm.sh BUILD/arch/x86/boot/bzImage NEW_IMAGE_DIR/initramfs.cpio.gz NEW_SMALL_RUN small
scripts/run-range-vm.sh BUILD/arch/x86/boot/bzImage NEW_IMAGE_DIR/initramfs.cpio.gz NEW_SCALE_RUN scale
scripts/run-range-vm.sh BUILD/arch/x86/boot/bzImage NEW_IMAGE_DIR/initramfs.cpio.gz NEW_NUMA_RUN numa
```

Use a new output path every time. `CC`, `BUSYBOX`, and `QEMU` select executables.
`CMDP_VM_TIMEOUT` sets the host watchdog in seconds (default 1200, allowed 60 to
3600). The scripts require access to `/dev/kvm` and read access to the host CMD-P
debug statistics. They do not alter device permissions or install software.

Each guest has 16 GiB RAM, four CPUs, and two explicitly described NUMA nodes
with CPUs 0–1 and 2–3. The runner requires at least 24 GiB of available host RAM
before launch. The guest independently checks its discovered CPU and node
topology; a one-node boot fails the suite. The host runner saves its boot ID,
kernel identity, and CMD-P counters before and after the run and requires exact
equality. It also requires zero host entries, managed pages, and isolated pages
before starting QEMU. Keep concurrent host CMD-P experiments stopped.

Run `small` before `scale` and `numa`. A nonzero result, incomplete serial log,
watchdog termination, kernel warning, or nonzero final ownership is a failure.
An in-guest process timeout cannot recover a wedged guest kernel: the host
watchdog is the recovery mechanism. Serial output and `qemu.exit` remain in the
run directory when the watchdog expires.

## Deterministic range tests

`cmdp_range_test` forks an unmonitored child and allocates one contiguous private
anonymous mapping in that child. This guarantees that the unaffected lower
addresses precede the victim tail. The child populates each resident page and
then blocks on a command pipe. The parent attaches a single-mm monitor, performs
one sweep, and verifies every populated fixture page is armed.

The child then makes only the selected victim range read-only with `mprotect`.
The test checks:

- Exactly the expected number of resident victim pages invalidates.
- Every victim's state is INVALIDATED; unaffected boundary pages stay ARMED.
- Address-dependent first/last-byte markers survive protection changes, and
  stores to both victim boundaries raise SIGSEGV while the VMA is read-only.
- After STOP, writes through the unaffected writable VMAs exercise their reused
  PTEs; restoring victim write permission permits verified stores to every page.
- Session generation counts conserve ownership before and after STOP.
- Range searches are bounded by victims plus a small number of empty searches.
- Address-tree comparisons per search obey the red-black tree height bound.
- STOP and target exit leave all global ownership counters zero.

The elapsed `mprotect` time and exact lookup work are emitted as
`CMDP_RANGE_METRIC` JSON. The work bounds avoid fragile runtime thresholds that
depend on hardware, debug instrumentation, or scheduling. Timing remains useful
for comparing runs of the same build and machine.

The small suite varies unaffected-prefix size with a fixed victim size, then
varies victim size with a fixed prefix. Head-range and unpopulated-tail controls
exercise earliest-hit and no-hit lookups. Automatic NUMA balancing is disabled
only inside this guest suite so unrelated notifier callbacks cannot invalidate
the exact-count assertions.

The scale suite holds a 65,536-page (256 MiB) victim range fixed while increasing
the total resident fixture population through 64K, 256K, 1M, and 2M pages. At the
largest point the fixture holds 8 GiB of application pages, plus kernel metadata.
It repeats head and empty controls at the largest prefix. Each case uses a fresh
session and exits its target before the next case starts.

## STOP while a scanner waits

`--stop-wait` uses the CONFIG_CMDP_TEST-only `CMDP_MON_TEST_HOLD_MMAP` ioctl to
hold the selected target's mmap read lock for two seconds. The test waits for
`test_mmap_readers` to confirm acquisition, starts a scanner, and waits for an
observed WAIT_MMAP phase plus a retry. A separate thread requests STOP.

The scanner must return to IDLE while the bounded reader is still holding the
lock. STOP must subsequently drain safely after that reader releases it. The
test checks STOP request/drain/completion timestamps, its request-time active
population, and zero final ownership. It does not require STOP to skip necessary
cleanup or declare success while pages remain owned.

## Actual NUMA callback overlap

The NUMA suite enables automatic NUMA balancing in the guest. The child first
touches 1 GiB on CPU 0 (node 0), then reads it on CPU 2 (node 1) for 45 seconds.
Periodic ARM sweeps run concurrently. The read loop does not change mappings,
fork, or write the fixture data.

Every read iteration verifies address-dependent markers on every page and its
exact checksum. Target-PID-filtered kprobes record `change_prot_numa` entry/return and
`cmdp_invalidate` entry. The test requires a notifier callback nested inside an
actual completed NUMA call and requires armed-page invalidation counters to
increase before STOP. The child is stopped and confirmed quiescent before trace
capture, so legitimate in-flight returns cannot be cut off. The trace must have
balanced entry/return counts, no malformed nesting, no buffer overruns/dropped
events, and no missed probes. Thus a two-node topology alone is not accepted as
NUMA coverage. Missing kprobe support or no observed overlap fails the gate rather
than silently skipping it.

This suite reserves 64 MiB of trace buffer per guest CPU (256 MiB total).
Per-page migration notifications can otherwise overwrite the outer NUMA calls.
The parser reads the entire trace, but emits at most 48 representative event
lines plus loss/probe/nesting summaries to serial. This bounds console work
without weakening the complete-trace checks.

## Existing regression coverage and limits

The small suite also runs the legacy and monitor selftests, hot/cold,
concurrent-write and churn fixtures, and four-thread generated-graph BFS. BFS
must report all eight expected PASS verifications. Existing tests continue to
cover concurrent write terminals and mapping lifecycle transitions. The new
range test does not manufacture otherwise unsupported aliases or claim that
all alias/fault/invalidation interleavings have been exhaustively exercised.

This suite is a correctness and liveness gate. It is not a measurement of native
Twitter BFS overhead, all writable mapping types, or dirty-bit optimization.
