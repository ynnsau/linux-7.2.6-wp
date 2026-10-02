# CMD-P v1 Changes — 2026-10-02

This document summarizes the restricted CMD-P write-protection prototype added on `cmdp-v1`.

## What changed

### 1. Physical-page CMD-P state

- Added `PG_cmdp` to `struct page` flags under `CONFIG_CMDP`.
- `PG_cmdp` is only a membership hint: authoritative state lives in a PFN-keyed XArray.
- Each `cmdp_entry` tracks the physical page, owning `mm`/session, generation, references, completions, aliases, LRU isolation, and state:
  - `ARMING`
  - `ACTIVE`
  - `REVOKING`
  - terminal cleanup

### 2. Persistent PTE write protection

CMD-P-managed pages are prevented from becoming writable again through the main PTE publication paths:

- `set_ptes()`
- x86 `ptep_set_access_flags()`

`cmdp_preserve_write_protect()` resolves the PTE to its physical page and keeps the PTE read-only while `PG_cmdp` is set.

### 3. Page arming path

`cmdp_arm()` performs the restricted v1 protection flow:

```text
validate VMA/page
→ resolve physical page
→ discover supported aliases with rmap
→ acquire VMA exclusion
→ isolate folio from LRU
→ publish ARMING entry
→ begin write_protect_seq
→ write-protect every supported alias
→ synchronous TLB invalidation
→ final pin/reference validation
→ end write_protect_seq
→ dummy hardware publish
→ ACTIVE
```

Only private anonymous order-0 pages in the supported single-mm scope are accepted.

### 4. First-write revoke path

`do_wp_page()` now recognizes CMD-P-managed pages.

```text
CPU write
→ write fault
→ lookup cmdp_entry
→ drop PTL
→ ACTIVE → REVOKING
→ dummy hardware revoke/drain
→ remove CMD-P membership
→ return to normal Linux write-fault handling
```

CMD-P does **not** directly make the PTE writable and does not replace Linux COW/reuse semantics.

### 5. Concurrent writers

The first writer atomically owns `ACTIVE → REVOKING`.

Additional writers wait on completion instead of issuing duplicate revokes or spinning.

### 6. Lifetime and mapping cleanup

- Retains a page reference while managed.
- Keeps the folio isolated from the LRU through `ARMING → ACTIVE → REVOKING`.
- Uses an MMU notifier to revoke before supported mapping destruction.
- Drains remaining entries when the owning `mm` exits.
- Uses generation numbers so stale completions cannot affect a newer arm of the same PFN.

### 7. Restricted-v1 MM exclusions

While CMD-P entries are active, unsupported MM changes are rejected conservatively:

- `fork`
- `mprotect`
- `mremap`

Other unsupported areas remain explicitly outside v1.

### 8. Userspace interface

Added `/dev/cmdp` as a privileged `miscdevice`.

UAPI:

```c
struct cmdp_page_req {
    __aligned_u64 addr;
    __aligned_u64 flags;
};

CMDP_IOC_ARM
CMDP_IOC_REVOKE
```

Normal userspace control now uses `ioctl(2)`.

Debugfs remains for statistics, pin/GUP fixtures, failure injection, delayed revoke, and stale-completion testing.

### 9. Testing and validation

The selftest suite now covers 18 cases, including:

- arm/read
- first-write revoke
- same-PFN behavior
- explicit revoke
- concurrent writers
- pin/reference rejection
- partial-arm failure
- repeated arm/write cycles
- stale completion rejection
- `mprotect` rejection
- `munmap`
- mm exit
- invalid ioctl inputs
- foreign-mm/session rejection

Validation results:

```text
targeted builds: PASS
CONFIG_CMDP enabled/disabled builds: PASS
full vmlinux build: PASS
W=1 / CONFIG_WERROR: PASS
QEMU boot: PASS
runtime selftests: 18/18 PASS
lockdep-enabled QEMU run: PASS
final counters: entries=0 managed=0 isolated=0
```

A runtime issue where immediate re-arm could fail because LRU putback remained in a per-CPU batch was fixed by draining that batch before re-isolation when needed.

### 10. Benchmarking

Added `cmdp_bench.c` and `BENCHMARK.md`.

The benchmark measures:

- ordinary store baseline
- end-to-end `CMDP_IOC_ARM`
- first write/revoke
- kernel arm time
- kernel revoke time

Current QEMU medians:

```text
ioctl ARM:          5,960 TSC ticks
first write/revoke: 3,840 TSC ticks
kernel arm:         2,435 ns
kernel revoke:        651 ns
```

These are software-path measurements in QEMU, not physical CMD-P/CXL hardware measurements.

## Main files

```text
include/linux/cmdp.h
include/linux/cmdp_pte.h
include/uapi/linux/cmdp.h
include/linux/page-flags.h
include/linux/pgtable.h
arch/x86/mm/pgtable.c
mm/cmdp.c
mm/memory.c
mm/mmap.c
mm/mprotect.c
mm/mremap.c
tools/testing/selftests/cmdp/
wp_doc/
```

## Current v1 boundary

Implemented:

- explicit physical-page arming
- supported alias write protection
- first-write revoke
- concurrent revoke serialization
- supported mapping/mm cleanup
- ioctl userspace control
- tests and software-path timing

Not implemented:

- real CMD-P/CXL hardware communication
- automatic page selection
- generic DMA support
- arbitrary kernel direct-map writers
- multiple mms
- THP
- KSM
- UFFD
- `FOLL_FORCE`
- device migration
- general active fork/mprotect/mremap support

## Next stage

The software write-protection mechanism is now a validated baseline.

The next research step is to define the real CMD-P hardware publish/revoke contract and integrate it without weakening the existing ordering and lifetime guarantees.
