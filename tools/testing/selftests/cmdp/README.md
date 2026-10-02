# CMD-P v1 prototype

This is a restricted native x86-64 physical-page write-protection experiment.
It has no real CXL/CMD-P device protocol. The primary control interface is the
privileged `/dev/cmdp` miscdevice, with `CMDP_IOC_ARM` and `CMDP_IOC_REVOKE`
ioctls taking a page-aligned address and zero flags. Its fd binds one CMD-P
session to the opener's mm. Debugfs remains available for statistics and
`CONFIG_CMDP_TEST` fixtures; the selftest uses it only for those purposes.

## Implemented design

`PG_cmdp` indicates that CMD-P manages a physical page and suppresses new
writable PTE publications. An XArray keyed by PFN owns a `cmdp_entry` with a
retained page, owning mm/session, generation, references, completions, and
`ARMING`, `ACTIVE`, or `REVOKING` state. The entry owns LRU isolation while
managed. Terminal teardown clears the page flag, removes the XArray entry,
returns the folio to the LRU, and drops the manager reference.

Arming holds mmap write exclusion, resolves the page, walks reverse mappings
to collect supported aliases, and excludes VMA writers. It isolates the folio
and registers the entry before beginning `write_protect_seq`. For each alias it
uses the PTE lock to write-protect the PTE and synchronously invalidate the
TLB. After a final ordered pin/reference check, it ends the sequence, invokes
the dummy publish completion, and moves to `ACTIVE`. The PTE publication
filters preserve read-only protection while `PG_cmdp` is set.

The first CPU write reaches `do_wp_page()`. One fault owner changes `ACTIVE` to
`REVOKING`; concurrent writers wait. The dummy hardware revoke/drain completes
before registry and page-flag teardown. CMD-P does **not** make a PTE writable:
the fault retries, and normal Linux write-protection, COW, and reuse rules
decide what happens next. An MMU notifier revokes affected entries before
supported mapping destruction and drains remaining entries on mm release.

## v1 scope

Supported: private anonymous order-0 pages, one known mm/session, explicit
manual arming, ordinary CPU writes, supported aliases within that mm,
concurrent CPU writers, munmap/mapping destruction, process exit, and a dummy
immediate-completion hardware backend.

Unsupported: real CXL/CMD-P communication, automatic read-mostly selection,
generic DMA integration, arbitrary kernel direct-map writers, multiple mms,
THP, KSM, UFFD, `FOLL_FORCE`, device migration, fork while `ACTIVE`, mprotect
while `ACTIVE`, and mremap while `ACTIVE`.

## Validation status

The full `vmlinux` build passed with `W=1` and `CONFIG_WERROR=y`; the CMD-P
selftest and benchmark built, and an isolated QEMU x86-64 guest booted with
`CONFIG_CMDP=y`. All 18 runtime selftests passed, including invalid/out-of-range
ioctl requests, unsupported mappings, unmanaged revoke, closed-fd behavior,
64 repeated arm/revoke cycles, concurrent writers, munmap, and process exit.
Final manager counters were `entries=0 managed=0 isolated=0`.

The 1,000-sample QEMU benchmark methodology and measured results are in
[BENCHMARK.md](BENCHMARK.md). Those timings measure the software prototype,
not physical CMD-P hardware.

Runtime testing found that immediate re-arm could fail while LRU putback was
still queued in a per-CPU batch. Re-arm now drains that batch when necessary
before isolation. A separate QEMU kernel with `CONFIG_PROVE_LOCKING` and
`CONFIG_DEBUG_ATOMIC_SLEEP` passed the same 18 tests without lockdep or atomic
sleep reports. Real hardware coherence/drain semantics remain unimplemented.
