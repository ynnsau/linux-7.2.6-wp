# Periodic process-memory monitor

This implementation adds `/dev/cmdp_monitor` alongside the original single-page
`/dev/cmdp` interface. A privileged external controller binds a pidfd to one
address-space lifetime. A delayed kernel worker inventories writable mappings,
arms eligible resident pages, leaves survivors armed, and retries eligible OFF
pages on subsequent sweeps. The default period is 10 seconds, measured from
start to start; sweeps cannot overlap and overruns are reported.

The first milestone supports exclusive private-anonymous 4 KiB pages with THP
disabled for the target process or VMA. Shared/file mappings, huge pages,
nonresident pages, aliases, pins and other unsupported cases are counted as
exclusions. Residency is known only for inspected supported VMAs. The dummy
backend does not establish protection against arbitrary device or DMA writers.
Active pages remain isolated from the LRU under the prototype's existing
physical-page contract.

The monitor obtains resident page references under the PTE lock, without
faulting pages in or marking them accessed. This matters because a normal GUP
lookup adds FOLL_TOUCH and may queue activation references, causing the strict
pin/reference eligibility check to reject references created by the scanner
itself. Pending LRU additions are drained before each sweep. The pin/reference
guard is retained.

First writes revoke one ARM generation before normal Linux write handling
continues. Mapping changes withdraw affected ownership while preserving the
application operation. Fork, exec, target exit, controller close and changes to
the supported THP/KSM/UFFD policy stop and drain the attachment. Application
threads sharing the selected process address space are supported; children and
unusual non-thread mm sharing are not followed.

The versioned ABI exposes ATTACH, START, SCAN_ONCE, STATUS, STOP, PAGE and COHORT.
Aggregate and cohort counters distinguish first ARM, re-ARM, active survivors,
write revocation and other terminal causes. Survivor timestamps are preserved.
Protected byte-time uses byte-milliseconds. Histories and cohorts are bounded;
resource failures are explicit. Per-address queries expose the latest episode,
not a complete page-event stream or complete OFF-time distribution.

The userspace launcher disables THP only in its child before exec. It attaches
after successful exec and records the launch-to-attachment gap. BFS source does
not need readiness hooks. Graph and algorithm allocations can therefore appear
gradually in the inventory. The controller can also attach to an existing pidfd
target without changing its THP policy.

See [MONITOR.md](../tools/testing/selftests/cmdp/MONITOR.md) for commands and
report semantics. The deterministic selftest uses private test barriers for its
100 nominated pages while the monitor still scans the entire target process.
The BFS runner performs a separate verified run, then enough real trials for
at least 20 completed 10-second sweeps. Twitter-scale monitoring and overhead
measurements require activating the installed kernel; a small synthetic BFS
test in a VM is not a Twitter result.

Dirty-bit selection, adaptive re-ARM policy, broader mapping support and
performance optimization remain later stages.
