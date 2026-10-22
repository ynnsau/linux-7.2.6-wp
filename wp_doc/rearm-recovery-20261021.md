# CMD-P ARM recovery: 2026-10-21 build and experiment handoff

**Build status:** the diagnostic-only defect reproduction and all six suites
on both debug and production images have passed. The full production build completed successfully with 5,072 modules.
Installation verification and native recovery measurements
remain **pending**. The production image is `7.2.6-cmdp-rearm-production`; it is not yet installed or booted. GRUB selection and reboot remain with the user.

## Problem and resulting behavior

An external invalidation can end CMD-P ownership while leaving the mapping's
read-only PTE unchanged. The previous dirty sampler required an ordinary writable
PTE, so a read-only application could leave these pages untracked indefinitely.
Low WP counts in that condition do not establish successful coverage.

The recovery implementation accepts ordinary read-only PTEs for a **fresh**
observation epoch. It does not restore write permission. The transition is:

```
ARM -> external invalidation -> fresh observation and flush -> clean for Q -> ARM
```

On x86, ordinary writable entries carry dirty evidence in the hardware D bit;
ordinary read-only entries can carry it in `SAVED_DIRTY`. The sampler requires
RW entries to have no saved-dirty bit and RO entries to have no raw D bit. It
harvests only the encoding's matching dirty bit, preserves folio dirty accounting,
and completes a synchronous TLB flush before publishing a fresh quiet epoch.
Final admission rechecks the same predicate and requires no dirty evidence.
Read-only-plus-raw-D and writable-plus-saved-dirty encodings are rejected. The
scalar test hook exercises these helpers without installing hazardous PTEs.

The existing scope remains in force: writable VMAs containing supported,
exclusive, private anonymous 4 KiB pages. Shared/file mappings, aliases, pinned
pages, huge pages, userfaultfd and shadow-stack mappings remain excluded. This is
not a claim that every writable process mapping is covered.

Diagnostics now separate sampling rejection reasons and attribute invalidations
to their recorded kernel path. Callback attribution is stronger evidence than
an undifferentiated invalidation total, but is not necessarily the ultimate
cause of the operation. The earlier native PR run did not collect this new
attribution; the VM results do not retroactively identify every prior PR loss.

Source and test references:

- [CMD-P implementation](../mm/cmdp.c)
- [Userspace ABI](../include/uapi/linux/cmdp.h)
- [Deterministic and NUMA recovery protocol](../tools/testing/selftests/cmdp/RECOVERY.md)
- [Native preparation and measurement protocol](../tools/testing/selftests/cmdp/RECOVERY-NATIVE.md)

## Source and artifact identity

The implementation freeze is commit
`0b4d4a2a6174dc5de227252e5d900121b5fd9cb8` (`cmdp: requalify ordinary read-only pages
after invalidation`). The diagnostic-only baseline freeze is
`049e666980285a94df27f19d4f3de55253e9a744`. Later test, verifier and documentation
commits may advance HEAD without changing the kernel inputs.

The remote branch is `cmdp-rearm-recovery` in
[ynnsau/linux-7.2.6-wp](https://github.com/ynnsau/linux-7.2.6-wp). Native preparation additionally pins the original Twitter graph in commit `5d798a217`. The implementation and tools
are kept as separate commits, including:

| Commit | Purpose |
| --- | --- |
| `b050d918f` | Invalidation attribution and bounded recovery diagnostics |
| `db8d42770` | Deterministic stranded-page reproducer and recovery fixtures |
| `0b4d4a2a6` | Ordinary RO recovery and matching dirty-bit harvesting |
| `3e2596c8a` | Dirty encoding and permission-boundary selftests |
| `7e01c4b02` | Require baseline plus six debug and six production VM gates |
| `0bdfe8cfa` | Independently recheck debug and baseline evidence before installation |
| `d99bd5cb7` | Reject newer incomplete VM attempts instead of selecting an older pass |
| `ee06e9c23` | Prepare the native BFS/PR comparison and analysis tools |

The evidence root on BMP is
`/home/yans3/cmdp-v2/periodic/rearm-recovery-20261021`. Its
`records/baseline-kernel-source.json` and `records/fixed-kernel-source.json` pin
hashes of six kernel inputs: `mm/cmdp.c`, `mm/mprotect.c`, `mm/mremap.c`,
`include/linux/cmdp.h`, `include/trace/events/cmdp.h`, and
`include/uapi/linux/cmdp.h`. The build verifier checks the fixed hashes against
current source and the committed blobs at the freeze; it does not confuse a
later tools-only commit with a new kernel implementation.

| Artifact currently verified | SHA-256 |
| --- | --- |
| Immutable diagnostic-only debug `baseline-artifacts/bzImage` | `51b6da0a0352871b9affb2501c8037a215399df45f1c9538175938844616cb08` |
| Recovery `build-debug/arch/x86/boot/bzImage` | `5572f3839b02b4dc8371433b2af9c47efb9711cc1fcdcab47a44e390c65c2183` |
| Debug `.config` | `69b462ec7f94b7161f10ed5c055f473b091ee8ffaae5bf48f05d04f4c007b1cd` |
| Production image | `9de55b107d65a2c5a5457e86f0008faf7f8f8c1ce584485f7a3aea6c2160b323` |
| Production GNU build ID | `af98a59cd997005cf396c829f814f03a1986c6b4` |
| Production module count | 5072 |

The debug release is `7.2.6-cmdp-rearm-debug`, with `PROVE_LOCKING`,
`DEBUG_ATOMIC_SLEEP`, and `DEBUG_VM` enabled. No production image digest or module
count is inferred from the debug build. The full build exited zero and its final
image hash is identical to the production VM input. Nine frame-size warnings in
unchanged Mellanox/RDMA drivers exactly match the previous dirty-kernel build;
there are no CMD-P warnings or compiler/make errors.

An initial baseline initramfs preparation attempt failed while collecting
shared libraries for both GAPBS binaries. Commit `cb2865303` corrected collection
per binary. `initrd-baseline-v2` passed; the failed attempt remains in the logs.
No failed attempt is counted as a passing VM gate.

## Observed VM results

These are disposable KVM guests with four vCPUs, two NUMA nodes, and 16 GiB RAM.
Their recorded host guards kept BMP on `7.2.6-cmdp-dirty-production`, boot ID
`a27a890b-9239-4f42-864a-1a0561b1f242`. Guest NUMA tuning did not change host settings.

| Evidence directory | Observed result | TAP checks / workload validation |
| --- | --- | --- |
| `debug-recovery-baseline-v1` | Defect reproduced as explicitly expected | 17 checks at one thread + 17 at four threads |
| `debug-recovery-v1` | Strict recovery passed | 62 checks at one thread + 86 at four threads; generated BFS and PR, each at one/four threads, 8 verified trials per cell |
| `debug-recovery-numa-v1` | Actual NUMA invalidation and recovery passed | Four local/cross-node cases; 13, 14, 13, 13 checks |
| `debug-small-v1` | Existing small regression passed | TAP plans `18,59,18,18,18,18,18,18,18,18,20`; hot/cold, concurrent and churn fixtures; 8 verified generated BFS trials |
| `debug-scale-v1` | Existing scale regression passed | Six TAP suites of 18 checks |
| `debug-numa-v1` | Existing NUMA regression passed | 20 checks |
| `debug-dirty-v1` | Existing dirty regression passed | TAP plans `18,59,18,20,96`; observe/qualify BFS at one/four threads, 8 verified trials per cell; actual swap exercise |
| `production-{dirty,small,scale,numa,recovery,recovery-numa}-v1` | All six passed | 780 TAP checks and 72 verified generated GAPBS trials; recovery 62/86, NUMA-recovery 13/13/13/13 |

Every completed report above has `pass: true`, no collected kernel warning
signatures, balanced global ARM/publish/revoke generations, and zero final
entries, managed pages and isolated pages. Explicit negative tests can increment
reject/stale counters; these are not incorrectly described as zero everywhere.

The baseline is an expected-failure reproduction, not fixed-kernel acceptance.
In both thread cases, the same PFN and exact ordinary RO flags
`0x8000000000000825` survived no-op invalidation and subsequent application reads.
Ownership became zero; the observation stayed invalid across four additional
full-Q scans, and read-only rejection counters increased.

The fixed deterministic fixture uses Q=200 ms and explicit completed scans. It
requires a new epoch, refuses early admission, and recovers all 64 fixture pages
without application stores. Recorded invalidation-to-recovery latency was
approximately 614.6 ms for one thread and 444.4 ms for four threads. These
intervals include deliberate sleeps and fixture scan scheduling; they are not
fault-service measurements or native recovery-latency predictions.

The same fixture checks writes after recovery, writes during recovery, genuine
`mprotect(PROT_READ)`, same-address replacement, `MADV_DONTNEED`, migration of an
unowned recovery observation, concurrent writers, STOP and target exit. ARMED
folios remain LRU-isolated; the migration test releases ownership before asking
Linux to migrate. The four-writer phase remains active until the controller
explicitly finishes its invalidation/scans. Saved-dirty testing confirms the same
PFN and exact PTE flags with only the saved-dirty bit cleared, then fresh-Q ARM.

Actual automatic NUMA tests first-touch 128 MiB on guest node 0, run 15 seconds
of read-only work on the selected node, and observe the following source counters:

| Threads | Read placement | NUMA callbacks | ARM generations revoked by NUMA | Recovery evidence |
| ---: | --- | ---: | ---: | --- |
| 1 | Local node 0 | 7 | 32,770 | Fixture page 0 regained a distinct qualified generation |
| 1 | Cross-node, node 1 | 13 | 65,541 | Fixture page 0 regained a distinct qualified generation |
| 4 | Local node 0 | 15 | 63,903 | Fixture page 0 regained a distinct qualified generation |
| 4 | Cross-node, node 1 | 18 | 65,545 | Fixture page 0 regained a distinct qualified generation |

These counts are generations across the monitored process, not unique fixture
pages. A page may contribute more than once. The NUMA cases demonstrate an actual
kernel-path invalidation and subsequent qualified recovery of the checked page;
they do not assert complete coverage at every instant. The deterministic fixture
provides the stronger all-fixture-page fresh-epoch guarantee.

## Production build and installation gates

[verify-rearm-build.py](periodic-host-scripts/verify-rearm-build.py) requires the
baseline reproduction, all six debug suites, and all six production suites. It
checks actual image/input hashes, serial-report integrity, complete TAP evidence,
host guards, source freezes and final build artifacts. Numeric newest-attempt
selection includes incomplete run directories, so a newer failed attempt cannot
be hidden by an older passing report.

The [no-GRUB installer](periodic-host-scripts/rearm-install/README.md) requires an
explicit reviewed final build-report SHA-256. It independently rechecks those
gates, requires zero host ownership and absent versioned destination paths, then
uses direct versioned image copies, `modules_install`, and `mkinitramfs`. It does
not invoke bootloader-update commands or reboot.

Installation acceptance must establish unchanged GRUB contents/recorded metadata,
existing boot links/files, running kernel, boot ID and CMD-P counters; exact module
paths/counts; signed sample module payloads matching stripped build modules; the
signing certificate embedded in the installed image; and extracted NVMe initramfs
module bytes matching the installed modules. The planned installation evidence
root is `/home/yans3/cmdp-v2/periodic/native-install-rearm-recovery-20261021`.

**At this draft checkpoint, no production installation success is claimed.**
After installation is verified, the user chooses GRUB and reboots. Native tests
must not run until a fresh postboot audit confirms the candidate's running GNU
build ID against the hash-validated `vmlinux`, not merely its release string.

## Native experiment handoff — prepared, not executed

The new `prepare-recovery-native.py`, `run-recovery-native.py`, and
`analyze-recovery-native.py` scripts are committed. Their offline/static checks
are preparation evidence, not native experimental results.

After user activation, the protocol requires fresh small regression gates and
separate verified Twitter trials before measurement. It retains the existing
directed Twitter graph (61,578,415 vertices, 1,468,364,884 edges,
12,732,173,745 bytes), requires the original study graph checksum and binds that full graph hash, and records binary/source,
boot, image/build-ID and tool provenance for each phase. Q and sweep period remain
10 seconds; observation includes process launch, graph load, initialization,
complete algorithm trials, exit and STOP.

PR uses one complete converged trial at one thread and four at four threads,
with `-i 1000 -t 1e-4`. BFS selects and freezes whole-trial counts from fresh
pilots targeting approximately four minutes of total monitored elapsed time.
No partial trial is truncated to force a duration. This longer observation is
needed because the earlier single-thread PR coverage loss occurred after the
old two-minute window.

The analysis will report live ARM, admission/WP/invalidation rates, source
attribution, rejection reasons, protected GiB-seconds and sampled zero-coverage
spans. CPU progress is bound to the actual BFS/PR executable and required thread
count. Consecutive zero samples cannot rule out ARM episodes between samples;
CPU progress does not distinguish graph load, algorithm work and teardown.

There is no matched unmonitored baseline in this recovery protocol. Kernel
write-helper or invalidation-helper elapsed time covers only that helper and can
include contention. It is not the complete architectural fault cost or
end-to-end application overhead; overlapping helper times must not be added as
though they were disjoint application time. Native coverage and performance
findings remain pending until the candidate is booted and measured.
