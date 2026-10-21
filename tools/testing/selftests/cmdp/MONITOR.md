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

Reports distinguish write revocation from mapping/exit/control cleanup.
Already-armed pages retain their original generation and timestamp. Cohort
counts are checked for conservation. The current ABI provides per-address
queries and cohort aggregates, not a complete page-event stream; reports do
not invent complete per-page armed/off duration distributions. Protected
byte-time is reported in byte-milliseconds to avoid rapid nanosecond overflow.
`resident_pages` counts only pages inspected in supported VMA types. Unsupported
VMA extents are counted as skipped virtual pages; their residency is unknown.
Reports do not use this counter as an all-process resident-memory denominator.
