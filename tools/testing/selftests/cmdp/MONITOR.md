# External periodic CMD-P monitor

`cmdp_monitor` uses `/dev/cmdp_monitor`; the existing `/dev/cmdp` tools and
18-check legacy selftest are unchanged. The controller and fixture require
Linux x86-64, root, and the matching monitor-enabled kernel/UAPI.

Build with `scripts/build-monitor.sh`; set `STATIC=1` for initramfs binaries.
The regular kselftest Makefile also builds the three new programs. Only
`cmdp_monitor_test` is an automatic test; the controller and workload fixture
are auxiliary programs.

```
scripts/test-monitor.sh --case all --out results/functional
build/cmdp_monitor --period-ms 1000 -- build/cmdp_fixture --mode hotcold --seconds 5 > monitor.jsonl 2> target.log
build/cmdp_monitor --once --inventory --pid PID
scripts/run-bfs.sh --gapbs-dir /path/to/gapbs --graph /path/to/twitter.sg --threads 1 --out results/bfs
```

Launch mode disables THP in the child before `exec`, then attaches using a
pidfd after exec succeeds. There is no application checkpoint/readiness hook.
Attach mode does not change an existing target's THP policy. Unsupported
policy/mapping/page cases remain visible in reason counters. The first
milestone supports one process and its threads, private anonymous resident
order-0 pages. This is not a claim to protect every writable mapping type.

Periodic scanning is done by the kernel. Cadence is start-to-start; scans do
not overlap. The controller reports status and preserved cohorts as JSONL.
Its active/history bounds default to 8 million pages, enough for the Twitter
CSR rather than silently stopping at the kernel's smaller default. Resource
failures and dropped reports make the controller fail. `--once` performs one
synchronous scan and drains; `--inventory` reports without ARM. On a normal
duration/sweep limit, a launched workload is allowed to finish after protection
is stopped. Signals stop the monitor and terminate a launched target; attach
mode never sends signals to the existing target.

Before STOP, the controller emits `observation_end` and
`cohort_observation_end` snapshots. These are sequential observations while
the target can still run, not an atomic application checkpoint. It then emits
`stop_request` with a userspace monotonic timestamp. A dedicated reporter
thread continues `stopping_status`, `progress`, new `sweep` records, and cohort
snapshots while the main thread blocks in STOP. Complete JSON records are
serialized, and the shared session descriptor remains open until the reporter
has joined. `stop_return` records elapsed time and errno; `final` and
`cohort_final` describe the state after STOP. Cleanup never overwrites the
observation-end snapshots. A reporter thread cannot guarantee recovery if the
kernel itself prevents STATUS or STOP from progressing.
Target exit or another kernel lifecycle event can initiate cleanup before the
controller reaches `observation_end`. Therefore reports keep the controller
observation-end population and kernel STOP-request population separate; only
`stop_request_active` captures that earlier automatic-stop boundary.

The additive `CMDP_MON_PROGRESS` ioctl leaves the original status/cohort
layouts unchanged. The controller omits progress on older kernels returning
ENOTTY. Progress reports the scanner phase (`idle`, `prepare`, `wait_mmap`, or
`scanning`), current and completed mmap-wait time, retries, in-flight and
completed range operations, address-tree comparisons, and maximum index-lock
hold time. Range timing covers invalidation/modify callbacks including history
updates and revocation. Its total includes completed callbacks only; an
in-flight operation is separately counted. Index lock timing excludes lock
acquisition contention. Kernel STOP timestamps distinguish the initial request
and active-page snapshot, drain start, and completion. `write_calls`, `write_ns`,
and `write_max_ns` cover `cmdp_prepare_write()` including waiters, not the entire
architecture page-fault path and not application runtime overhead. Write calls
need not equal write-terminal generations. All nanosecond timestamps use the
monotonic clock.
The existing status field `pre_stop_active` remains the drain-start snapshot
for compatibility; use progress `stop_request_active` for the earlier request
boundary. `stop_complete_ns` means ownership has drained and may precede the
STOP syscall return. Progress phase/range fields and address-index statistics
are sampled under separate locks, so a progress record is not globally atomic
while work is running.

`cmdp_monitor_test` uses test-only pipe barriers and per-address queries for
deterministic assertions. The monitored process is still scanned in full.
Assertions are scoped to its 100 nominated pages because stack/runtime pages
also write. Cases include 100 cold survivors, 25 writes leaving 75 survivors,
exactly 25 fixture rearms, concurrent writers, ordinary mapping changes,
nonresident discovery, fork/exec/exit stops, and stop cleanup. Fixtures use
ordinary mappings with process-wide THP disable, not MADV_NOHUGEPAGE.

BFS is unmodified. The runner first performs a separate monitored `-n 1 -v`
correctness run in `verify/`, requiring the controller's accounting checks and
the BFS verifier's PASS before starting the long observation. The BFS runner
sizes the trial count from a short runtime
measurement unless `--trials` is supplied. It requires at least 20 completed
sweeps at 10 seconds and at least 200 seconds of reported trial computation.
A short run fails validation; sleeps never inflate workload duration. This
is functional/runtime sizing, not an overhead comparison. The verification
run's allocations and writes are excluded from the long run's metrics.

The runner writes `manifest.json` before monitored verification starts and
updates its stage through verification, runtime sizing, and observation. The
manifest explicitly records that monitoring begins at process launch and
includes graph loading/initialization. The compute-duration requirement uses
BFS's reported trial times; it does not prove that every reported sweep
occurred during a trial. Ordinary script errors and handled signals attempt
partial validation/report generation. SIGKILL, a stopped host, or a blocked
shell can prevent that cleanup; after recovery, run `validate-run.py` and
`report-run.py` manually against the saved directory.

Incomplete runs always fail strict validation. Valid records before a truncated
JSON line are retained. `completed_sweeps` uses the last observed kernel count
(or completed cohort evidence) when final status is missing, and is null if
unknown. Reports label final state and cleanup outcome unknown rather than
inventing zero completed work or zero remaining pages. Last-observed cohorts
may have different observation times; they are not a final synchronized
snapshot. `validation.json` also retains observation-end cohorts, last progress,
and STOP request/return records for diagnosis.

Reports distinguish write revocation from mapping/exit/control cleanup.
Already-armed pages retain their original generation and timestamp. Cohort
counts are checked for conservation. The current ABI provides per-address
queries and cohort aggregates, not a complete page-event stream; reports do
not invent complete per-page armed/off duration distributions. Protected
byte-time is reported in byte-milliseconds to avoid rapid nanosecond overflow.
`resident_pages` counts only pages inspected in supported VMA types. Unsupported
VMA extents are counted as skipped virtual pages; their residency is unknown.
Reports do not use this counter as an all-process resident-memory denominator.
