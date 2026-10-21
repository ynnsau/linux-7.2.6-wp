---
type: design-proposal
status: implementation-sequence-defined
created: 2026-10-20
updated: 2026-10-20
tags:
  - cmd-p-write-protect-study
  - gapbs
  - profiling
---

# Periodic process-wide ARM design

Study home · Validated GAPBS setup and earlier controlled pilot

> [!important] Implementation priorities confirmed
> Make periodic scanning, ARM/re-ARM and reporting work first for one process and its threads. Measure and reduce overhead afterward. Dirty-bit selection and soft-dirty/UFFD comparison experiments are deferred. The executable order and planned test scripts are in Periodic ARM implementation sequence; it supersedes the earlier observation-first sequence below.

## Objective and scope

The research objective is now a kernel-managed policy that periodically examines an application's memory, arms eligible pages, observes how long they remain armed, and revisits pages after writes. The application should keep its ordinary memory behavior and eventually require no readiness signal. BFS remains the first workload. A period near 10 seconds is an initial candidate, not an established operating point.

This supersedes the after-initialization checkpoint as the main design. That checkpoint remains useful as an optional controlled diagnostic. No new kernel or BFS implementation, dirty-tracking run, or periodic-ARM measurement has been performed for this proposal.

The existing repository plan requires **all writers to an advertised physical page** to be covered: user mappings, kernel writers, and devices. Retain this as the eventual CMD-P contract. Measuring writes through one process's mappings is a useful preliminary experiment, but does not establish that physical-page guarantee. [Repository contract](https://github.com/ynnsau/linux-7.2.6-wp/blob/be39a758b48011620694612dd9a35a8446cf179d/wp_doc/PLAN.md)

Use writable VMAs to define the initial process-memory inventory, including heap, stacks, TLS, runtime and allocator data. Do not select only PTEs that are currently writable: COW and our own ARM may already have cleared their write permission. Count separately:

- Writable virtual bytes, resident backing bytes, and absent/swapped pages.
- Pages eligible for this implementation, successfully armed pages, and rejected/unsupported pages by reason.
- Virtual mappings and unique physical pages where aliases exist; never silently count aliases as distinct physical capacity.

The first real-CMD-P stage may support only a subset, but its denominator and exclusions must remain visible. Do not prefault the entire address space merely to make the experiment cover more pages. Newly populated pages can enter at the next sweep initially; their unobserved interval and pages born and freed between sweeps are limitations. Admission at first population would need additional hooks.

## Policy and state

The initial policy is deliberately simple:

1. At each scheduled pass, discover new eligible pages and retry eligible pages that are currently off.
2. Keep existing survivors armed. Do not revoke/re-arm them or reset their original ARM time.
3. On the first covered write demand, complete CMD-P revocation before allowing the ordinary write path to proceed. Record a single owner transition even if several threads fault.
4. Leave the page off until a later pass. A page can become useful again after a write burst ends.
5. Record unmap, permission changes, COW, migration/reclaim, exit and administrative withdrawal separately from writes.

Conceptually: `OFF -> ARMING -> ARMED -> REVOKING -> OFF`. OFF records carry their cause and any cooldown deadline. Each successful ARM starts a new generation. A later policy can increase cooldown for pages that repeatedly fault soon after ARM, or select candidates after one or more quiet observation windows.

Separate **sweep period**, **per-pass work budget**, and **cooldown** as policy parameters, even if the first experiment retries every off page each sweep. Re-arming immediately after every fault would repeatedly fault on hot pages and is not the proposed baseline.

For rolling scans, each page's active interval begins only when its protection/publication completes. A process-wide sweep is not an instantaneous snapshot. Record pass start/end, per-page or batch ARM times, schedule overruns, and missed deadlines. Proposed scheduling: start-to-start deadlines, no overlapping passes; count an overrun rather than launching competing sweeps.

## Results to produce

For each period Δ and cooldown policy, report the following as counts and bytes where applicable:

| Result | Meaning |
| --- | --- |
| Coverage | Total resident writable population, eligible population, successfully armed population, and exclusions |
| Current armed amount | What remains active at a particular time; distinguish from newly armed cohort survival |
| Survival curve | Fraction of an ARM cohort still continuously armed after 1, 10, 30 seconds, etc.; separate first ARM from re-ARM |
| Transition counts | New ARM, re-ARM, first-write revocation, non-write invalidation, and exit |
| Re-ARM usefulness | Time from write to next ARM; fraction surviving a chosen duration after re-ARM; repeated short-lived ARM episodes |
| Armed byte-seconds | Integral of active bytes over time, with a time-varying resident/eligible denominator reported separately |
| Cost | Scan/ARM CPU time and elapsed time, fault/revoke work, TLB effects, bookkeeping memory, total runtime/throughput change |

For a fixed successfully armed cohort, reconcile `initial cohort = still active + write-revoked + otherwise retired` at each observation time. Keep later admissions in their own cohorts. A page that unmaps is not a survivor and is not automatically a write. Use competing-event counts, and label any incomplete observation rather than treating it as proof of a write-free interval.

Track process/mm identity, mapping lifetime, virtual address, backing-page lifetime and ARM generation. Bare virtual addresses or PFNs can be reused. A malloc object may be freed while its mapping remains, so mapping history is not object history. Physical-page identity is authoritative for CMD-P publication; virtual mapping records support application attribution.

The intended output is a family of curves: `armed byte-seconds(Δ)`, `survival(age | Δ, first/re-ARM)`, `transitions(Δ)` and `slowdown(Δ)`. Candidate periods could be 1, 3, 10 and 30 seconds once sweep costs are known. Retain a longer period or limit work if a full pass cannot finish in time. Do not extrapolate the existing single-page measurements into full-process timing claims.

No writes alone do not establish optimization benefit: an unused page may remain armed indefinitely. If the eventual benefit requires reads, add a separately measured read-activity signal or benefit model. Accessed/young information can help with this distinction but does not identify writes by itself.

## Kernel work

**External control.** Add an attach/configure/query/stop interface for a privileged controller, preferably using a stable process handle such as pidfd plus explicit mm lifetime handling. Start with one process and all its threads. Keep controller buffers outside the target mm. A sleepable worker can perform bounded batches; a timer should schedule that work, not perform a long page-table walk in timer context. A workqueue versus a dedicated thread is an implementation choice, not the core interface.

**Concurrent protection.** Refactor the ARM implementation to operate on an explicit target mm rather than assuming `current->mm`. Preserve per-page synchronization, PTE/TLB ordering, publication completion, cancellation and fault revalidation. No application-wide readiness barrier is needed for a rolling policy once these concurrent transitions are correct. Do not infer that `mmap_lock` stops all stores by application threads.

**Application transparency.** Preserve VMA permissions and normal COW/fault semantics. Replace prototype-wide prohibitions on normal operations with revoke-before-operation or another validated lifetime protocol. Fork/exec, mapping changes, memory pressure and exit must work normally. Unsupported pages should be skipped with accounting, rather than breaking the application. This is substantial MM integration, not merely adding a periodic thread.

**All-writer coverage.** Before real publication, interlock alias creation, writable pins, supported kernel access paths and device writers with revocation. A user PTE cannot protect a direct-map or DMA writer. Shared/file-backed pages, huge pages and external writers need explicit support or exclusion. Do not weaken this contract merely because a page has appeared quiet.

**Accounting and overhead.** Add per-session reason-specific counters and compact transition/age statistics. Small correctness runs can retain full events; performance runs should avoid per-fault printing. Support bounded scans and compact metadata. Measure memory-pressure effects and eventual reclaim-triggered revocation: the current prototype isolates active pages from the LRU, which can bias a long-running experiment.

Current `cmdp-v1` admits only resident, exclusive, private anonymous base pages under restrictive VMA conditions, requires `VM_NOHUGEPAGE` when THP is configured, and excludes shared/file/KSM/UFFD and large folios. Commands are tied to the caller's mm; there is a single global owner. It is not yet an arbitrary-process periodic service. [Current eligibility and ARM](https://github.com/ynnsau/linux-7.2.6-wp/blob/be39a758b48011620694612dd9a35a8446cf179d/mm/cmdp.c#L382), [current session checks](https://github.com/ynnsau/linux-7.2.6-wp/blob/be39a758b48011620694612dd9a35a8446cf179d/mm/cmdp.c#L745)

## Dirty bits and alternative observations

| Mechanism | Useful information | Limits for this project |
| --- | --- | --- |
| Hardware PTE dirty bit | A mapping was dirtied after a correctly synchronized reset; possible candidate-selection signal without intentional first-write WP faults | No pre-write interception or exact write time; no guarantee about other aliases/devices; clearing costs and Linux dirty accounting still matter |
| Soft-dirty | Existing process-level interval write tracking controlled externally through `/proc` | Reset itself removes write permission and causes first-write faults; mapping changes can appear dirty; read/reset is not a whole-process atomic observation |
| Async userfaultfd WP plus PAGEMAP_SCAN | Kernel auto-resolves WP; scan and re-protect operations provide a relevant bulk-tracking reference | Requires feature checks and range registration in the target context; not an arbitrary PID attach API and not CMD-P physical publication |
| CMD-P ARM | Active read-only window with ordered revocation; precise transition timestamps possible | Requires the all-writers/lifetime contract and measures its own protection cost |

Linux soft-dirty resets through `/proc/PID/clear_refs`, then exposes interval state in pagemap. New/expanded mappings are marked dirty conservatively. This is a practical first comparison with no BFS source changes, but it is neither free hardware-dirty polling nor a measurement of actual CMD-P advertisement lifetime. [Soft-dirty documentation](https://docs.kernel.org/6.14/admin-guide/mm/soft-dirty.html)

Hardware dirty-bit sampling would need its own audited kernel implementation: safe page-table updates, TLB synchronization, preservation of normal dirty/writeback state, and handling of teardown or other kernel clearing. A raw bit clear can lose observations. Existing `mapping_dirty_helpers.c` illustrates record/clear and flush ordering, but is limited to particular shared mappings and is not a drop-in arbitrary-process sampler. Hardware PTE dirty state, soft-dirty, and folio/PageDirty are different signals. [Same-tree implementation precedent](https://github.com/ynnsau/linux-7.2.6-wp/blob/be39a758b48011620694612dd9a35a8446cf179d/mm/mapping_dirty_helpers.c)

Async userfaultfd WP and `PAGEMAP_SCAN` are worth a separate baseline if their setup costs are acceptable. Registration, mapping churn and feature availability must be checked on bmp; documentation alone does not establish runtime support. Do not stack independent WP mechanisms on CMD-P pages until coexistence has been designed and tested. [Pagemap scan workflow](https://www.kernel.org/doc/html/v6.9/admin-guide/mm/pagemap.html#pagemap-scan-ioctl), [userfaultfd async behavior](https://docs.kernel.org/6.14/admin-guide/mm/userfaultfd.html)

Dirty history is most useful for choosing **which OFF pages to try next**. Once a page is ARMED, CMD-P's state provides the active-window information. A quiet past predicts a longer next window; it does not guarantee one, so actual protection remains necessary.

## BFS role and earlier experiment alternatives

Keep the stock GAPBS binary as the primary workload. The earlier dedicated-array allocation changes and initialization readiness hook are optional diagnostics, not required by this design. Optional phase/range labels may explain graph versus algorithm memory, but must not control whether the mechanism works.

The selected order is now external kernel control/lifecycle, scan-only inventory, periodic dummy ARM and accounting, deterministic tests, then unmodified BFS. The implementation sequence defines completion checks and expected scripts. External soft-dirty/UFFD comparisons, hardware dirty-bit sampling, period tuning and history-based policies remain later alternatives rather than prerequisites.

The recorded Twitter BFS setup trial took **0.22197 s**, so one trial cannot characterize a 10-second policy. Increase real work using GAPBS's existing `-n` repetitions; choose a count from measured runtimes to span, for example, at least 20 completed periods. Do not add sleeps. Retain real per-trial allocation/destruction and allocator reuse. The graph persists across repetitions, whereas BFS parents/frontiers and worker scratch change frequently. [Recorded validation](Results/20261020-gapbs-twitter-setup/records/validation.json), [BFS source](https://github.com/sbeamer/gapbs/blob/941462c7c42e786e4e006a9211b0576ef78d23f7/src/bfs.cc), [trial lifecycle](https://github.com/sbeamer/gapbs/blob/941462c7c42e786e4e006a9211b0576ef78d23f7/src/benchmark.h#L97)

Twitter's graph storage is approximately 12.7 GB and largely read-only during BFS; it will dominate a combined survivor count. Report the whole monitored population plus meaningful mapping/lifetime groups; use optional diagnostic labels if exact graph/state attribution is needed. Stock `-v` adds substantial verifier work, so verification activity must not be mistaken for BFS traversal. Existing stock validation remains valid but is not a periodic-ARM result.

## Working assumptions and remaining questions

- Final goal: transparent periodic management of ordinary application memory, with eligible coverage expanded explicitly and all-writer safety for actual CMD-P publication.
- Confirmed controller target: one process/mm and all its threads; no process-tree or cgroup enrollment in this implementation.
- Initial sweep: around 10 seconds, rolling while the application runs, keeping surviving pages armed and retrying off pages later.
- Initial measurement: no persistent checkpoint image, no application readiness dependency, and no forced prefault of all reserved memory.
- Confirmed priority: make scanning, ARM/re-ARM and reporting work before setting an overhead budget or optimizing it.
- Deferred: dirty-bit selection and soft-dirty/UFFD comparison experiments.
- Pending implementation details: scan work budget, mapping-change hooks, physical writer coverage, history storage, and coexistence with other WP facilities.
