# Per-session periodic scanner CPU selection

The paired BFS placement study showed timer-driven work could leave the launcher CPU. This interface makes scanner placement explicit and independently observable without changing shared kworker affinity or the target application.

## Interface

`CMDP_MON_CPU_CONFIG` accepts `cpu = -1` (AUTO, legacy policy) or an online CPU ID. Configure after ATTACH and before the first synchronous scan or START. Reconfiguration after scanning begins returns EBUSY; an offline CPU returns ENODEV. START rechecks online state. Explicit placement rejects synchronous SCAN_ONCE with EOPNOTSUPP, because it executes in the ioctl caller.

The controller accepts `--scanner-cpu auto|N`. An omitted option remains compatible with older kernels; an explicit option requires the new ABI. Existing ioctl layouts and terminal-reason arrays remain unchanged. Placement failure uses existing MON_ERROR plus the negative errno in CPU_STATUS.

`CMDP_MON_CPU_STATUS` reports requested CPU, last observed worker-entry CPU, work item count, wrong-CPU entry count, offline events, and placement error. These are entry diagnostics, not CPU-time measurements; scheduling traces are still needed to measure contention during a scan.

## Queueing and teardown

Both the initial work item and every delayed requeue use schedule_delayed_work_on for explicit placement. CPU-online protection is held only around configuration/queueing, with cpus_read_lock before ctl. It is never held over the scan, mmap acquisition, or synchronous work join.

Each open monitor registers a CPU-hotplug instance. Removing its selected CPU atomically marks a running session STOPPING with MON_ERROR/-ENODEV, removes its pending pinned timer, and queues cleanup on system_dfl_wq. The callback never waits for scanner work: a scanner can itself wait for hotplug through LRU draining. Cleanup joins the scanner and performs the existing ownership drain. In-flight cancellation/flush/drain may finish on another CPU after hotplug; the monitoring run is already invalid, and no new sweep continues there. CPU restoration does not restart the session.

Close first removes the hotplug instance, then joins scanner work, then joins deferred CPU cleanup, then frees session/history. This prevents callbacks or a late scanner error from queuing cleanup after the object is freed. STOP and close retain the existing drain semantics; the target application stays alive.

AUTO retains the old periodic policy. Dirty sampling and ARM remain one combined work item. Batch limits, dirty certificates, TLB flush-before-publication rules, mmap cancellation, and WP recovery are unchanged.

## Validation and activation

The CPU-only fixture refuses native execution: it requires root, cmdp_suite=cpu in the guest command line, and exactly four online CPUs. It covers ABI errors, placement on guest CPU1/2 with one/four application threads, eight delayed requeues, observe/qualify, write/re-ARM, mprotect/remap, STOP/exit, and CPU removal with live ARM or a batch paused before flush. STOP/offline and close/offline each race three times. Launcher cases check explicit placement and an expected CPU-offline failure with a normally exiting target.

Run the CPU suite and the existing fused, batch, dirty, small, scale, NUMA, recovery and recovery-NUMA suites on both the debug and production images. CPU removal tests run only in disposable VMs. Versioned native installation must preserve GRUB, old kernels, boot symlinks and running boot ID. The user controls GRUB selection and reboot.

## Next native comparison after reboot

Hold BFS on CPU1 (one thread) or CPU1-4 (four threads), controller on CPU5, and collectors on CPU24/25. Compare scanner CPU1 with scanner CPU0. Run matched fixed work with monitoring off, sampling-only, and dirty-to-ARM, with three repetitions and rotated mode order. Keep graph, batch limits, cadence and NUMA policy fixed; record actual scanner execution CPUs, runtime, RSS, live ARM exposure, WP count/time, TLB and scheduling evidence. Low end-to-end overhead with substantial ARM coverage remains the design priority. CPU separation alone does not eliminate shared cache, memory bandwidth, IPIs or lock contention.
