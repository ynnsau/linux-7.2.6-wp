# CMD-P v2: consolidated write-protection study

Status: proposed plan grounded in this source tree; no prototype or measurements
have been implemented. The tree's Makefile identifies it as 7.2.6.

## Objective

Replace CMD-P v1's manually identified read-only regions with temporary read-only
windows managed by Linux. Select a candidate page, prevent writes, advertise it
to CMD-P, and revoke that advertisement before allowing a subsequent write.
Applications should retain their normal read/write semantics.

Confirmed requirement: **all writers to the underlying physical page matter**.
While a page is advertised, no CPU mapping, kernel access path, or device may
modify its contents. Revocation must complete before any such write is allowed.
The hardware entry's address encoding remains a separate interface question.

Separate two questions: **can we establish and revoke these windows correctly
and cheaply?** Then, **which pages stay useful long enough to justify the cost?**
Implement explicit selection first; add automatic selection after measuring the
mechanism. A history without writes predicts usefulness; it does not guarantee
that the next access will be a read.

## What existing Linux mechanisms tell us

| Question | Finding in this tree | Source |
| --- | --- | --- |
| How does x86 protect a mapping? | Architecture helpers clear the PTE write permission while preserving architecture-specific state. Changing the PTE also requires the appropriate TLB invalidation; stale writable translations must be gone before CMD-P publication. | [pgtable.h](../arch/x86/include/asm/pgtable.h), `pte_wrprotect()` and `ptep_set_wrprotect()`; [mprotect.c](../mm/mprotect.c), `change_pte_range()` |
| Can userspace request protection now? | `mprotect(PROT_READ)` changes VMA permissions as well as mappings. A later userspace write is an access violation, normally SIGSEGV, rather than a transparent CMD-P event. | [mprotect.c](../mm/mprotect.c), `mprotect_fixup()`; [fault.c](../arch/x86/mm/fault.c), `access_error()` |
| Is there an existing transparent WP interface? | Synchronous userfaultfd WP lets a handler observe a write, perform bookkeeping, and remove protection before the writer continues. It is a useful functional baseline, with userspace notification/scheduling overhead. | [userfaultfd documentation](../Documentation/admin-guide/mm/userfaultfd.rst), “Write Protect Notifications”; [userfaultfd.c](../mm/userfaultfd.c), `uffd_wp_range()` |
| Who else installs protection? | Fork protects private COW mappings; soft-dirty tracking clears write permission to observe subsequent writes; userfaultfd installs explicit WP state. File-backed mappings also have write-fault bookkeeping. A read-only PTE alone does not identify its owner. | [memory.c](../mm/memory.c), `__copy_present_ptes()` and `wp_page_shared()`; [soft-dirty documentation](../Documentation/admin-guide/mm/soft-dirty.rst) |
| Does a WP fault always copy? | No. `do_wp_page()` can reuse an exclusive anonymous page through `wp_page_reuse()`. It calls `wp_page_copy()` when private-mapping semantics require a copy. CMD-P must preserve those semantics. | [memory.c](../mm/memory.c), `do_wp_page()`, `wp_page_reuse()`, `wp_page_copy()` |
| Where would CMD-P handling belong? | The relevant present, PTE-mapped write-fault path reaches `do_wp_page()` through `handle_mm_fault()` and `handle_pte_fault()`. This is an integration point to investigate, not a complete design for every fault type. | [fault.c](../arch/x86/mm/fault.c), `do_user_addr_fault()`; [memory.c](../mm/memory.c) |

The intended combination is **writable VMA + temporarily non-writable PTE +
explicit CMD-P ownership state**. Do not infer CMD-P ownership from a cleared
write bit, appropriate userfaultfd's bit, or bypass legitimate COW.

## Initial prototype scope

Use one cooperating process and explicitly registered, private anonymous,
base-page mappings. Write-touch pages before registration so they have private
backing rather than the shared zero page. Disable THP for the test range and
reject unsupported mappings. Start with one application thread, then test
multiple writers and CPUs.

Initially exclude shared/file mappings, KSM, huge pages, device mappings,
userfaultfd overlap, and externally pinned/DMA-accessed pages. Treat soft-dirty
interaction as an explicit coexistence decision before enabling that experiment.
Checking these properties once is not a lifetime guarantee.

The first tests may prohibit fork, remapping, migration/reclaim, and external
writers as controlled experiment conditions. Such restrictions must be reported
with results; supporting general workloads requires enforcement or invalidation
hooks, not just test-program discipline.

One writable alias can invalidate the physical-page read-only guarantee. CPU
PTE protection alone also does not block writes through kernel aliases or DMA.
Keep publication simulated until the following eligibility contract is enforced.

### Physical-page eligibility contract

- Track ownership and active state for the backing physical page, with a lifetime
  identity/generation that prevents PFN reuse from inheriting an old entry.
  Per-mm virtual-address records provide lookup and mapping associations; they
  cannot independently authorize publication of the same physical page.
- Cover every user mapping. Initially reject pages with unsupported aliases;
  later, supporting aliases requires coordinating protection and TLB invalidation
  across all affected address spaces. Serialize admission with creation of new
  mappings and writers, rather than relying on a one-time mapcount check.
- Account for existing writable pins and in-flight device writes before arming.
  Reject pages whose writers cannot be excluded or quiesced, and interlock new
  writable GUP/pin access with revocation. A one-time pin check is insufficient.
- Audit kernel paths that can modify candidate pages, including access through
  the direct map. User PTE faults cannot intercept these writes. Each supported
  path must revoke first or be prevented from accessing an active page. Private
  anonymous ownership alone does not establish this guarantee.
- Revoke before migration, freeing/reuse, or other lifecycle operations invalidate
  the physical-page identity or introduce a writer. Stabilizing page lifetime
  alone does not prevent writes.

This is a correctness requirement for admitted pages, not a promise to support
every memory type initially. If a writer cannot be covered, the page is ineligible
for real publication. Mapping-level baselines remain useful for measuring part
of the mechanism, but cannot establish physical-page safety.

## Protection and revocation contract

Conceptual states: `UNTRACKED -> ARMING -> ACTIVE -> REVOKING -> UNTRACKED`.
These states describe required ordering, not a chosen metadata representation.

1. **Arm:** establish physical-page eligibility under appropriate synchronization;
   record CMD-P ownership and page/mapping identities; remove write permission
   from every supported CPU mapping using architecture helpers; complete required
   TLB shootdowns and writer quiescence; then publish the CMD-P entry and mark it
   active. Prevent admission of new writers throughout this transition.
2. **First write or writer admission:** recognize CMD-P state and revalidate the
   physical-page identity (and the mapping for a fault); revoke the
   CMD-P entry and complete the hardware-defined invalidation/drain operation;
   only then permit Linux to restore write access and retry the instruction.
   For eligible exclusive anonymous pages, this should reuse the same page.
3. **Other invalidation:** revoke before a mapping/backing-page change makes the
   entry stale, or before another path grants write permission. Remove metadata
   on unregister, unmap, and exit; handle address/PFN reuse with identity checks.
4. **Failure or competing WP owner:** withdraw CMD-P eligibility and retain normal
   Linux protection/fault semantics. Never grant write permission merely because
   a CMD-P record existed.

A concurrent fault must not restore write access while an arming thread later
publishes the same entry. Serialize transitions, including cancellation and
duplicate faults. Do not hold a PTE spinlock across a sleeping operation. If the
eventual hardware revoke can sleep or wait significantly, design a retry/wait
path with mapping revalidation before restoring access.

Use dummy publish/revoke hooks and optional `pr_info` for the first correctness
demo. A printed message demonstrates the software sequence; it does not prove
hardware completion or ordering. Disable logging for latency measurements.

## Milestones and completion criteria

### 1. Establish userspace baselines

Create a small program that allocates and write-touches a page-aligned range.

- Measure a normal read/write baseline.
- Use `mprotect` to demonstrate protection and verify that a write faults with
  SIGSEGV in a controlled child process. Measure protection and explicit
  restoration separately; a signal path is not the target CMD-P fault path.
- Use synchronous userfaultfd WP, after checking kernel support and permissions,
  to demonstrate first-write notification, dummy revocation, unprotection, and
  successful retry. Prepopulation avoids confusing missing-page and WP faults.

Done when reads work while protected, writes trigger the expected mechanism,
and userfaultfd allows the original write to complete. A new syscall is not
needed to answer the initial userspace feasibility question.
These tests establish mapping-level behavior only; neither interface alone
enforces the all-writers physical-page requirement.

### 2. Add a kernel CMD-P mechanism with explicit registration

Implement one shared kernel arm/revoke mechanism and a userspace test control
interface, provisionally a device ioctl on the caller's own address range.
Decide metadata and locking before editing the fault path. Use physical-page
ownership/state with lifetime identity as the design requirement, plus per-mm
address records for fault lookup. The concrete representation is still open.

A module can provide test control, but plan for a small in-tree MM patch for
fault dispatch and lifecycle integration. `do_wp_page()` is static, and the
existing protection internals are not a general exported module interface. A
module that merely clears a PTE cannot supply the required CMD-P fault behavior.
If a dedicated syscall is later necessary, have it invoke the same mechanism.

Done when arming performs dummy publication after protection takes effect;
reads proceed; the first write performs dummy revocation before access is
restored; subsequent writes do not fault until rearmed; and an eligible page
keeps its backing PFN with no copy. Verify PFN identity in kernel instrumentation
and verify fault-path behavior; minor-fault counts alone cannot establish no COW.

### 3. Verify races and lifetime handling

Test repeated arm/write cycles, competing writers, arm-versus-write races, and
unregister/unmap/exit. Add explicit handling or rejection for fork, mprotect,
mremap, migration/reclaim, new pins, and other ways an active mapping can change.
Include an ordinary COW control case to verify process isolation remains intact.
Test attempted alias creation, writable pin acquisition, and supported kernel
write paths while armed: each must be rejected or complete revocation before
obtaining write access. Verify existing pins/aliases cause rejection when their
writers cannot be controlled. Include PFN reuse and cross-mm concurrency cases
when those transitions are supported.

Done when stale entries cannot survive supported transitions, concurrent faults
revoke each active generation once, and unsupported cases cannot silently become
advertised. Require a writer-coverage audit alongside tests: every supported way
to modify an admitted page must be excluded or ordered after revocation. This
gate precedes real hardware or general-workload claims.

### 4. Measure costs

Start with userspace TSC timing for end-to-end costs, then add kernel tracing to
explain them. Keep baseline, stub CMD-P, and eventual real MMIO results separate.

| Metric | Measurement boundary |
| --- | --- |
| Protection cost | Control-call entry to return, including lookup, locks, PTE updates, TLB shootdowns, and dummy publication |
| First-write cost | Ordered timestamps around a store into an armed page, including exception entry, revocation, retry, and return to userspace |
| Kernel handling cost | Trace boundaries around the CMD-P handling region; separately measure a wider MM-fault region if useful |
| Steady-state cost | Reads while armed and writes after revocation, compared with ordinary accesses |

For `RDTSCP`, select and document a fenced timestamp sequence appropriate to
the CPU and the metric. Bare `RDTSCP` does not establish every required ordering
boundary; specify whether the test measures store execution or global visibility.
Use compiler barriers, prevent store elimination, measure timestamp overhead,
pin the thread, and check CPU migration. Report TSC ticks rather than assuming
they equal core cycles; document any conversion to time.

For eBPF, first check build configuration, BTF/symbol visibility, and probe
eligibility. `do_user_addr_fault()` is marked `NOKPROBE_SYMBOL` in this tree;
`wp_page_reuse()` is inline. Do not promise kprobes on either. Prefer explicit
CMD-P tracepoints for path classification and timing; function probes on
available MM functions can supplement them. A broad fault duration mixes fault
types unless filtered, and function timing excludes some end-to-end overhead.

Run warmups and repeated trials; report median, p95, and p99 plus sample count.
Record CPU, kernel/config, page size, CPU affinity, and instrumentation settings.
Sweep range size/batching and then CPUs sharing the mm to expose shootdown costs.
Measure without tracing as well to quantify instrumentation overhead.

### 5. Select pages automatically

Define a tunable no-write observation window and cooldown after a write.
Soft-dirty provides a first experimental write-history signal, but clearing it
itself introduces WP faults and therefore has a measurement and runtime cost.
Account for that cost and its interaction with CMD-P before combining the two.
An accessed bit alone does not distinguish reads from writes; an untouched page
also has no writes but may offer no prefetch benefit.

Promote a candidate through the same validated arming path as manual selection.
Measure protected lifetime, write-fault rate, useful read activity, scanning
cost, and application performance. Tune thresholds only after comparing prefetch
benefit against protection, revocation, observation, and fault overhead.

## Hardware questions to settle before replacing the stub

- How does hardware encode an entry: physical page, virtual address plus
  address-space identity, or another identifier? Regardless of encoding, the
  confirmed protection requirement covers every writer to the physical page.
- What publication ordering is required after CPU write protection completes?
- Does revocation synchronously invalidate/drain outstanding CMD-P work? What
  completion must the CPU observe before allowing the original write?
- What are entry granularity/capacity and behavior on eviction, context switch,
  address reuse, and page migration?

The immediate next deliverable is the milestone 1 baseline program and a short
design for milestone 2's metadata, locking, and control interface. Keep automatic
selection and real MMIO behind their respective correctness and measurement gates.
