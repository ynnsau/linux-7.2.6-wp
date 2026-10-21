---
type: implementation-plan
status: implementing
created: 2026-10-20
updated: 2026-10-20
tags:
  - cmd-p-write-protect-study
  - gapbs
  - profiling
---

# Periodic ARM implementation sequence

Study home · Design rationale · Verified GAPBS setup

## Decisions

- **Confirmed:** make the mechanism work correctly first; measure and reduce overhead afterward. Runtime and sweep duration are recorded from the start, but there is no performance acceptance threshold yet.
- **Confirmed:** one selected process/address space and its threads. No process-tree or cgroup enrollment.
- **Confirmed:** implement scanning, ARM/re-ARM and reporting now. Hardware dirty-bit sampling, soft-dirty/UFFD comparisons, history-based selection and adaptive cooldown are deferred.
- **Retained:** dummy CMD-P backend; first-write revocation; survivors remain armed; pages revoked by writes become candidates at the next pass; BFS stays source-unmodified.
- **Confirmed first coverage:** inventory all writable mappings, ARM eligible resident exclusive private-anonymous 4 KiB pages, and report every exclusion category. This is a first milestone toward wider coverage, not a claim to protect every writable byte.
- **Confirmed test setup:** a launcher sets full process-level THP disable before exec. No system-wide THP change. Kernel admission must recognize the matching mm-level condition and still reject large folios.

Both final scope/setup choices are accepted. The user authorized implementation, build and installation on bmp, and committing current changes. Implementation is underway; no runtime result is implied until recorded below or in a linked validation record. The existing no-reboot instruction remains in force.

## Milestone 1: kernel control and lifecycle foundation

Work in a separate branch/worktree from `cmdp-v1` at `be39a758b48011620694612dd9a35a8446cf179d`. Preserve the existing single-page ioctl behavior and regression suite. Keep changes in the existing in-tree CMD-P implementation; a separate module is not needed for this milestone.

1. Add an external monitoring session interface with attach, start, status, single-pass and stop operations. Bind it to a stable process handle and an explicit mm identity; require the existing privileged access plus appropriate target access checks. Permit one active monitored mm initially. Give the UAPI a version/size and reject unsupported flags.
2. Separate controller identity from target mm. Refactor current-mm assumptions in ARM and page lookup so a worker can operate on the target. Use kernel APIs appropriate to remote-mm lookup; do not just invoke current-mm GUP from a kernel thread. Hold live references only while required and revalidate mappings before arming.
3. Define session states such as ATTACHED, RUNNING, STOPPING and STOPPED. Controller close/failure, explicit stop, target exit and fatal setup errors prevent new ARM operations and drain existing ownership. Preserve the final report after drain until it has been collected.
4. Bind an attachment to one mm lifetime. Exec ends that attachment instead of silently following a new image. A fork that creates a new mm ends monitoring and drains ownership before cloning; allow the application operation and report the reason. Do not follow children. Same-mm application threads remain supported; unusual cross-process mm sharing needs explicit handling and must not silently expand scope.
5. Establish lock ordering before adding MM hooks. Lifecycle callbacks under mmap exclusion cannot acquire a command mutex held by a worker waiting for that exclusion. Mark stopping before joining work; never synchronously cancel a worker while holding its needed locks or from its own teardown path.

Completion: attach/stop can be repeated; invalid/dead targets and permission failures are deterministic; target exit/exec and controller death cannot leak entries, page references or LRU isolation. Legacy selftests still pass.

## Milestone 2: scan and inventory, with ARM disabled

1. Add a sleepable delayed worker and a resumable VMA/page-table cursor. Walk writable VMAs, including stacks, heap, TLS and runtime allocations, rather than selected BFS arrays. A PTE currently lacking write permission is not automatically excluded: VMA semantics, ownership and backing-page eligibility determine handling.
2. Inspect resident pages without faulting in reserved/unpopulated memory. Record nonresident/swapped pages separately. Record unsupported mapping/page types and transient failures with distinct reason codes. Do not split huge pages or change allocation behavior during the scan.
3. Release locks between bounded batches, reschedule as needed and revalidate after unlocking. Store addresses/identities rather than retaining unprotected VMA pointers. A memory-map change can cause a retry or classified exclusion; it cannot leave a dangling pointer.
4. Implement configurable start-to-start cadence, initially 10 seconds. First pass starts after attachment/start. Never overlap passes. If work overruns, record elapsed time and missed deadlines, then schedule the next future deadline. A full pass means the scanner reached its end, not an atomic snapshot of a continuously changing address space.
5. Export each pass's timestamps, cursor/completion status, observed pages/bytes, eligibility and rejection counts. Include a scan-only mode for diagnosis and later overhead comparison.

Completion: a changing-memory fixture can allocate, unmap and change mappings while repeated scan-only passes finish with valid reports. An overrun cannot create overlapping workers or silently omit a partial pass.

## Milestone 3: periodic ARM, first-write handling and re-ARM

1. Connect the scanner to the existing dummy ARM/revoke ordering. Revalidate page identity and eligibility at the ARM operation itself. Keep already-armed entries unchanged; skip duplicate ARM rather than treating it as a new failure or resetting age.
2. Keep existing physical-page ownership plus a per-mm address/lifetime index for reporting and invalidation. Retain history after revocation without retaining the physical page solely for history. Invalidate history on relevant mapping/backing lifetime changes. Do not infer identity from a bare reused address or PFN.
3. Successful first ARM and later re-ARM receive distinct ARM generations. Record `armed_at`, episode type, arm-pass ID, first terminal time and terminal cause. Count one write-owner transition per generation even with simultaneous writers. Later ordinary writes while OFF are not observed by this mechanism and are not presented as store counts.
4. Retry eligible OFF pages only on a later pass, at most once per pass. Do not immediately re-ARM a page that faults while its own scan batch is still being processed. New mappings/pages enter on a later scan; births and deaths between visits are outside coverage and must be acknowledged.
5. Preserve ordinary application memory operations. Replace current blanket mprotect/mremap restrictions with ordered withdrawal of affected CMD-P ownership, then normal Linux processing. Do not restore write permission blindly: original read-only, COW and other WP semantics remain authoritative. Ordinary unmap and supported kernel writes must continue to work.
6. If process-level THP disable is selected, recognize full mm-level disable as an alternative to per-VMA NOHUGEPAGE, while preserving collapse exclusion and rejecting large folios. Re-enabling THP or otherwise changing eligibility must first withdraw affected ownership or stop the session. Do not merely delete the existing THP guard.
7. Maintain the existing restrictive physical-page contract for the dummy milestone. Do not claim real device or arbitrary direct-map/DMA writer protection. Bound bookkeeping/isolated-page resources, report limits and partial coverage, and make failure cleanup reliable. Reclaim-aware general protection remains a later correctness expansion; active-page LRU isolation is recorded as a prototype limitation.

Completion: deterministic fixtures demonstrate persistent survivors, a first write, OFF time, later re-ARM and survival after a hot-to-cold transition. All admitted pages remain correct under the supported concurrent/lifecycle tests. No stock-BFS run starts before these checks pass.

## Milestone 4: reporting and validation

Implement accounting with the scanner and transitions, then finalize a stable machine-readable export before running application-scale tests.

- **Run metadata:** schema version, run/session ID, target PID and mm lifetime, kernel/config/build hashes, command and graph identity, page policy, worker count, requested period and attach time. Record any launch-to-attach gap.
- **Per-pass summary:** pass start/end and completed/partial status; pages examined; resident/eligible pages; newly armed, re-armed and already armed; exclusions/failures by reason; terminal events by cause; active amount; overruns.
- **Episode records:** lifetime ID, ARM generation, cohort/pass, first versus re-ARM, monotonic ARM/terminal times, reason and page size. Full event detail is for small correctness runs; application-scale exports can use loss-accounted event streams plus authoritative aggregate/cohort counters.
- **Reasons:** at least WRITE, MAPPING_INVALIDATION, CONTROL_STOP, TARGET_EXIT, EXEC, FORK and ERROR. Preserve a generic invalidation category when a more specific cause cannot be established; do not invent precision.
- **Derived results:** survivor counts/age buckets, first-ARM versus re-ARM survival, ARM/OFF transition counts, protected byte-seconds, scan duration and raw end-to-end runtime. Exact page-history claims require complete history; drops or ambiguous lifetime continuity must be marked.
- **Before and after stop:** save active/survivor state before administrative drain, then save cleanup counters afterward. Otherwise every successful experiment would misleadingly end with zero survivors.

At a consistent snapshot, every successful ARM generation belongs to exactly one of ACTIVE, WRITE-revoked or OTHER-terminal. Failed/cancelled ARM attempts are separate. Verify this per cohort and globally. Require explicit accounting of dropped records or counter saturation. Mapping-level and physical-page counts must not silently mix aliases.

## Milestone 5: tools and scripts to generate

Proposed locations: kernel-facing helpers and deterministic fixture under `tools/testing/selftests/cmdp/`; study orchestration under `gapbs-study/scripts/`. Names and command lines below are planned deliverables, not existing tools.

| Artifact | Purpose and required checks |
| --- | --- |
| `cmdp_monitor` | External C controller/launcher: execute a command or attach by stable process handle, start/stop periodic scanning, collect reports, preserve child exit status, and reliably clean up on interruption. Launch mode attaches after successful exec and records the actual observation start. |
| `cmdp_fixture` | Deterministic C workload with named test-page ranges, barriers and explicit phase commands. Exercises private base pages, multithreaded writers and mapping changes without depending on sleeps as its test oracle. |
| `build-tests.sh` | Build controller/fixture and current CMD-P selftests; record source and binary hashes. |
| `preflight.sh` | Verify running kernel/UAPI/config and privileges; identify target/page policy and THP settings; check available resources and no conflicting session. No boot or system-wide policy changes. |
| `test-functional.sh` | Run existing tests, then no-write survival, single write, repeated writes while OFF, scheduled re-ARM, repeated ARM cycles, hot-to-cold and newly populated pages. |
| `test-concurrency.sh` | Same-page simultaneous writers, writers on different pages, ARM-versus-write, scans-versus-allocation, and stop-versus-fault races. Assert one terminal owner per ARM generation; do not require a waiter event on every schedule. |
| `test-lifecycle.sh` | unmap/remap/address reuse, mprotect read-only/write restoration, allocator churn, ordinary syscall writes into monitored buffers, rejected mapping types, exec, fork/no-follow, exit, controller kill, invalid ioctls, resource failures and scan overruns. Check data/COW semantics and final ownership cleanup. |
| `run-bfs.sh` | Run small graphs first with one worker and then a fixed parallel team, followed by Twitter. Select enough genuine `-n` repetitions for a requested number of complete sweeps. Record all commands, output, metadata and reports. No inserted workload sleeps or BFS readiness hooks. |
| `validate-run.py` | Check schema, counter/cohort conservation, generation uniqueness, timestamp ordering, coverage status, event loss, required completed passes, workload verification and final cleanup. Produce explicit PASS/FAIL/incomplete results. |
| `report-run.py` | Produce `summary.json`, `sweeps.csv`, reason/count tables and survival/ARM-time figures. Keep rejected/unknown populations visible and separate write-triggered outcomes from retirement. |

Fixture assertions apply to its declared test pages even though the monitor scans the entire process. The fixture's stack and communication buffers may legitimately write. Generic minor faults are not the oracle for CMD-P write-revoke counts. Deterministic first-write tests use the single-pass control to establish the episode, then exercise scheduled mode separately.

Expected run folder: `metadata.json`, `sweeps.jsonl`, `cohorts.jsonl`, optional `events.jsonl`, `stdout.log`, `stderr.log`, `validation.json`, `summary.json` and derived CSV/figures. Use monotonic timestamps within a run and record a wall-clock origin for navigation. The actual serialization can be refined during implementation while retaining these semantics.

## Milestone 6: BFS acceptance

1. Keep upstream GAPBS and Twitter data unchanged. The optional launcher page policy is applied identically to comparable runs.
2. Run small graph correctness cases with one worker and a fixed parallel team. Then verify Twitter BFS under the periodic monitor; do not infer correctness from process exit alone.
3. For the long Twitter trial, begin with 26 OpenMP workers on the already established CPUs 0–25. Use a 10-second period and enough real BFS repetitions to capture at least 20 completed sweeps. Select repetition count from observed durations and confirm actual completed passes; rerun with more work if necessary. A scan overrun is reported, not treated as proof the target period was met.
4. Verification runs use `-v` and label its extra work. A separate long observation run can omit the verifier after matched correctness validation, so its allocations do not masquerade as BFS state.
5. Success requires valid graph results, completed reporting, no kernel warnings/hangs, explained exclusions, correct cleanup, and demonstrated re-ARM/survival in the deterministic fixture. Do not require every natural BFS run to show every transition type.

There is no slowdown target for this milestone. Retain raw timing for later analysis; defer governor/NUMA tuning, interval optimization, dirty-bit selection and cooldown optimization until correctness and reports are trustworthy.

Building and test validation precede any deployment handoff. The existing instruction not to reboot bmp remains in force; a required kernel activation is a separate handoff after the build and reviewable test evidence exist.

## Final scope decisions

1. Accepted: inventory of all writable mappings with ARM initially limited to supported resident private-anonymous 4 KiB pages, reporting other types explicitly.
2. Accepted: process-local THP disable in the launcher for the first BFS runs, without BFS source changes or a system-wide setting change.

No additional preference is needed for cadence or overhead tuning now: use 10 seconds, keep survivors ARM, re-ARM eligible OFF pages on later passes, and prioritize correctness.
