/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_CMDP_H
#define _UAPI_LINUX_CMDP_H

#include <linux/ioctl.h>
#include <linux/types.h>

struct cmdp_page_req {
	__aligned_u64 addr;
	__aligned_u64 flags;
};

#define CMDP_IOC_MAGIC	'C'
#define CMDP_IOC_ARM	_IOW(CMDP_IOC_MAGIC, 1, struct cmdp_page_req)
#define CMDP_IOC_REVOKE	_IOW(CMDP_IOC_MAGIC, 2, struct cmdp_page_req)

/* External single-mm monitor. Legacy /dev/cmdp requests above are unchanged. */
#define CMDP_MONITOR_VERSION 1
#define CMDP_MONITOR_F_SCAN_ONLY (1U << 0)
#define CMDP_MONITOR_DEVICE "/dev/cmdp_monitor"

enum cmdp_monitor_state {
	CMDP_MON_UNATTACHED, CMDP_MON_ATTACHED, CMDP_MON_RUNNING,
	CMDP_MON_STOPPING, CMDP_MON_STOPPED,
};
enum cmdp_monitor_reason {
	CMDP_MON_NONE, CMDP_MON_WRITE, CMDP_MON_INVALIDATE,
	CMDP_MON_CONTROL_STOP, CMDP_MON_TARGET_EXIT, CMDP_MON_EXEC,
	CMDP_MON_FORK, CMDP_MON_ERROR, CMDP_MON_THP_CHANGE,
	CMDP_MON_SCOPE_CHANGE,
	CMDP_MON_NR_REASONS,
};
enum cmdp_monitor_page_state {
	CMDP_MON_PAGE_UNKNOWN, CMDP_MON_PAGE_ARMED, CMDP_MON_PAGE_OFF_WRITE,
	CMDP_MON_PAGE_INVALIDATED, CMDP_MON_PAGE_STOPPED,
};
enum cmdp_monitor_skip {
	CMDP_MON_SKIP_VMA, CMDP_MON_SKIP_THP_POLICY,
	CMDP_MON_SKIP_NONRESIDENT, CMDP_MON_SKIP_PAGE_TYPE,
	CMDP_MON_SKIP_BUSY, CMDP_MON_SKIP_ALIASES,
	CMDP_MON_SKIP_PINS, CMDP_MON_SKIP_RESOURCE, CMDP_MON_SKIP_OTHER,
	CMDP_MON_SKIP_SWAPPED,
	CMDP_MON_NR_SKIPS,
};

struct cmdp_monitor_attach {
	__u32 size;
	__u32 version;
	__s32 pidfd;
	__u32 flags;
	__u32 period_ms; /* 0 selects 10000; minimum 10. */
	__u32 max_pages; /* Active limit; 0 selects 1048576. */
	__u32 max_history; /* Address-history limit; 0 selects 1048576. */
	__u32 reserved;
};

struct cmdp_monitor_pass {
	__u64 id, start_ns, end_ns, cursor;
	/* resident_pages is observed within inspected supported VMAs only.
	 * VMA/THP_POLICY skips are uninspected virtual pages, not nonresident.
	 */
	__u64 writable_pages, examined_pages, resident_pages, eligible_pages;
	__u64 first_arms, rearms, already_armed;
	__u64 skipped[CMDP_MON_NR_SKIPS];
	__u64 active, write_revoked, other_terminal;
	__u64 active_first, active_rearm;
	__u32 complete;
	__s32 error;
};

struct cmdp_monitor_status {
	__u32 size, version;
	__u32 state, stop_reason;
	__u32 target_pid, flags;
	__u32 period_ms, reserved;
	__u64 session_id, attach_ns, now_ns;
	__u64 passes_started, passes_completed, overruns;
	__u64 first_arms, rearms, active, pre_stop_active;
	__u64 terminals[CMDP_MON_NR_REASONS];
	__u64 protected_byte_ms; /* Saturates at U64_MAX; <1 byte-ms rounding/event. */
	__u64 history_entries, history_resets, resource_failures;
	__u64 reports_dropped;
	struct cmdp_monitor_pass last_pass;
};

struct cmdp_monitor_page {
	__u32 size, version;
	__u64 addr; /* Input: page-aligned target virtual address. */
	/* state is current history state; reason/terminal_ns describe the last
	 * ARM episode terminal. Later invalidation never rewrites that cause.
	 */
	__u32 state, reason;
	__u64 lifetime_id, arm_generation, pass_id;
	__u64 pfn, armed_ns, terminal_ns, arm_count;
};

/* Cohort counters remain updated after the corresponding scan completes. */
struct cmdp_monitor_cohort {
	__u32 size, version;
	__u64 pass_id; /* Input. ENOENT when not retained/never issued. */
	struct cmdp_monitor_pass pass;
};

enum cmdp_monitor_scan_phase {
	CMDP_MON_SCAN_IDLE, CMDP_MON_SCAN_PREPARE,
	CMDP_MON_SCAN_WAIT_MMAP, CMDP_MON_SCAN_SCANNING,
};

/* Additive diagnostic ABI: existing status/pass layouts stay unchanged.
 * Times are monotonic nanoseconds. range_* covers invalidation/modify
 * callbacks (including history updates), not only application write faults.
 * lookup_nodes counts address-tree comparisons, including unsuccessful finds.
 * index_lock_max_ns measures hold time, excluding acquisition contention.
 * write_* covers cmdp_prepare_write(), including waiters, not the whole
 * architecture fault handler. scan_lock_wait_ns is the current wait only;
 * total includes completed waits, including cancelled acquisition attempts.
 */
struct cmdp_monitor_progress {
	__u32 size, version, scan_phase, reserved;
	__u64 now_ns, phase_since_ns;
	__u64 scan_lock_wait_ns, scan_lock_wait_total_ns, scan_lock_retries;
	__u64 range_calls, range_inflight, range_ns, range_max_ns;
	__u64 lookup_calls, lookup_nodes, lookup_max_nodes, index_lock_max_ns;
	__u64 stop_request_ns, stop_request_active, stop_drain_ns, stop_complete_ns;
	__u64 write_calls, write_ns, write_max_ns;
	__u64 test_mmap_readers; /* Always zero without CONFIG_CMDP_TEST. */
};

/* Available only with CONFIG_CMDP_TEST; bounded deterministic contention. */
struct cmdp_monitor_test_hold {
	__u32 size, version, milliseconds, reserved;
};

#define CMDP_MON_ATTACH _IOW(CMDP_IOC_MAGIC, 16, struct cmdp_monitor_attach)
#define CMDP_MON_START _IO(CMDP_IOC_MAGIC, 17)
#define CMDP_MON_SCAN_ONCE _IO(CMDP_IOC_MAGIC, 18)
#define CMDP_MON_STATUS _IOWR(CMDP_IOC_MAGIC, 19, struct cmdp_monitor_status)
#define CMDP_MON_STOP _IO(CMDP_IOC_MAGIC, 20)
#define CMDP_MON_PAGE _IOWR(CMDP_IOC_MAGIC, 21, struct cmdp_monitor_page)
#define CMDP_MON_COHORT _IOWR(CMDP_IOC_MAGIC, 22, struct cmdp_monitor_cohort)
#define CMDP_MON_PROGRESS _IOWR(CMDP_IOC_MAGIC, 23, struct cmdp_monitor_progress)
#define CMDP_MON_TEST_HOLD_MMAP _IOW(CMDP_IOC_MAGIC, 24, struct cmdp_monitor_test_hold)

/* Optional hardware-dirty observation. Configure after ATTACH, before any scan.
 * Existing status/pass/page ABI and unconditional ARM default are unchanged.
 * OBSERVE never arms; QUALIFY requires quiet_ms since a synchronized reset.
 */
enum cmdp_monitor_dirty_mode {
	CMDP_MON_DIRTY_DISABLED, CMDP_MON_DIRTY_OBSERVE, CMDP_MON_DIRTY_QUALIFY,
};
struct cmdp_monitor_dirty_config {
	__u32 size, version, mode, quiet_ms; /* 0 selects 10000; minimum 10. */
	__u64 reserved[2];
};
/* All counters are cumulative. samples = dirty + clean counts successful
 * eligible PTE observations; priming visits also count in dirty/clean.
 * qualified counts first threshold crossings per observation epoch, including
 * OBSERVE mode (which does not ARM). unknown includes unsupported/busy probes
 * and unavailable history; late_dirty is a final-admission rejection.
 * reset_ns/flush_ns are subsets of sample_ns, all monotonic nanoseconds.
 * valid_observations counts retained OFF epochs, not pages proven clean now;
 * it becomes zero at STOP. metadata_bytes includes the complete history
 * payload for retained pages (160 bytes each in the grouped implementation),
 * not reserved slots, region headers, XArray or cohort allocator overhead.
 * CMDP_MON_BATCH_STATUS reports reserved history allocation separately. Samples never expand the
 * existing supported CPU-writer contract to arbitrary kernel/DMA writers.
 */
struct cmdp_monitor_dirty_status {
	__u32 size, version, mode, quiet_ms;
	__u64 now_ns, passes_started, passes_completed;
	__u64 samples, dirty, clean, primed, unknown, qualified, late_dirty;
	__u64 resets, flushes, reset_ns, flush_ns, sample_ns;
	__u64 invalidations, wp_resets, resource_skips, valid_observations;
	__u64 metadata_bytes;
};
/* Counters persist across epochs. epoch_id increments after every reset and
 * synchronous TLB flush; quiet_since_ns begins after that boundary.
 * valid describes uninterrupted observation, not the current hardware D bit.
 * It is zero after ARM, invalidation, uncertain observation, or STOP; old
 * timestamps remain available. qualified_ns is the first Q crossing in this
 * epoch, cleared on the next reset. A first visit always primes, never arms.
 */
struct cmdp_monitor_dirty_page {
	__u32 size, version;
	__u64 addr; /* Input: page-aligned target virtual address. */
	__u64 pfn, epoch_id, quiet_since_ns, last_sample_ns, last_dirty_ns;
	__u64 samples, dirty, resets, qualified_ns;
	__u32 valid, last_dirty;
};
#define CMDP_MON_DIRTY_CONFIG _IOW(CMDP_IOC_MAGIC, 25, struct cmdp_monitor_dirty_config)
#define CMDP_MON_DIRTY_STATUS _IOWR(CMDP_IOC_MAGIC, 26, struct cmdp_monitor_dirty_status)
#define CMDP_MON_DIRTY_PAGE _IOWR(CMDP_IOC_MAGIC, 27, struct cmdp_monitor_dirty_page)

/* Additive diagnostics. All rejection reasons are exclusive: their sum equals
 * unknown at this same snapshot (separate DIRTY_STATUS queries can race).
 * Sources describe the notification/callsite, not the ultimate physical cause.
 * MODIFY calls and their subsequent notifiers are separate events; arm_revoked
 * counts only generations actually terminated by that callback.
 */
enum cmdp_monitor_unknown {
	CMDP_MON_UNKNOWN_NONRESIDENT, CMDP_MON_UNKNOWN_SWAPPED,
	CMDP_MON_UNKNOWN_PROTNONE, CMDP_MON_UNKNOWN_READONLY,
	CMDP_MON_UNKNOWN_SAVED_DIRTY, CMDP_MON_UNKNOWN_PAGE_TYPE,
	CMDP_MON_UNKNOWN_ALIASES, CMDP_MON_UNKNOWN_PINS,
	CMDP_MON_UNKNOWN_FOLIO_BUSY, CMDP_MON_UNKNOWN_MAPPING,
	CMDP_MON_UNKNOWN_OWNED, CMDP_MON_UNKNOWN_RESOURCE,
	CMDP_MON_UNKNOWN_SEQUENCE, CMDP_MON_UNKNOWN_OTHER,
	CMDP_MON_NR_UNKNOWNS,
};
enum cmdp_monitor_invalidation_source {
	CMDP_MON_SOURCE_UNKNOWN, CMDP_MON_SOURCE_NUMA,
	CMDP_MON_SOURCE_PROTECTION, CMDP_MON_SOURCE_MPROTECT,
	CMDP_MON_SOURCE_MREMAP, CMDP_MON_SOURCE_UNMAP,
	CMDP_MON_SOURCE_CLEAR, CMDP_MON_SOURCE_TEST,
	CMDP_MON_NR_SOURCES,
};
struct cmdp_monitor_invalidation_stats {
	__u64 calls, arm_revoked, observations_reset, histories_reset;
	__u64 elapsed_ns, max_ns;
};
struct cmdp_monitor_diagnostics {
	__u32 size, version;
	__u64 now_ns, unknown;
	__u64 rejected[CMDP_MON_NR_UNKNOWNS];
	struct cmdp_monitor_invalidation_stats source[CMDP_MON_NR_SOURCES];
	/* At most 4096 summary trace records per attachment; aggregate counts
	 * continue after that cap. Disabled tracing consumes no record budget.
	 * trace_records_dropped is CMD-P cap suppression, not tracefs loss.
	 */
	__u64 trace_records, trace_records_dropped;
};
enum cmdp_monitor_pte_category {
	CMDP_MON_PTE_UNMAPPED, CMDP_MON_PTE_NONRESIDENT,
	CMDP_MON_PTE_PROTNONE, CMDP_MON_PTE_WRITABLE,
	CMDP_MON_PTE_READONLY, CMDP_MON_PTE_SAVED_DIRTY,
	CMDP_MON_PTE_UNSUPPORTED, CMDP_MON_PTE_SHADOW_STACK,
};
/* One-address diagnostic, not a process-wide atomic snapshot. mmap/VMA/PTL
 * exclusion stabilizes mapping and ownership during collection. Reading this
 * does not fault or touch target memory, change A/D bits or widen permissions.
 * Returns EAGAIN when mmap exclusion is busy; callers must bound retries.
 * pte_flags is x86 PTE flags (no PFN); pfn is 0 when no normal page exists.
 */
struct cmdp_monitor_page_snapshot {
	__u32 size, version;
	__u64 addr;
	__u64 now_ns, pfn, pte_flags, arm_generation, epoch_id;
	__u64 quiet_since_ns, last_sample_ns, qualified_ns;
	__u32 category, owned, history_state, observation_valid;
};
/* CONFIG_CMDP_TEST only. A bounded (1..1024 base pages) external protection
 * notification under mmap/VMA write exclusion; deliberately changes no PTE.
 */
struct cmdp_monitor_test_invalidate {
	__u32 size, version;
	__u64 start, end;
	__u64 reserved[2];
};
#define CMDP_MON_DIAGNOSTICS _IOWR(CMDP_IOC_MAGIC, 28, struct cmdp_monitor_diagnostics)
#define CMDP_MON_PAGE_SNAPSHOT _IOWR(CMDP_IOC_MAGIC, 29, struct cmdp_monitor_page_snapshot)
#define CMDP_MON_TEST_INVALIDATE _IOW(CMDP_IOC_MAGIC, 30, struct cmdp_monitor_test_invalidate)

/* CONFIG_CMDP_TEST only. Abstract flag selectors for a local scalar PTE test;
 * no process PTE is read or written. Output reports the real sampler predicate
 * and clearing of only the encoding's selected D/SD bit when supported.
 */
#define CMDP_MON_TEST_PTE_PRESENT (1ULL << 0)
#define CMDP_MON_TEST_PTE_RW (1ULL << 1)
#define CMDP_MON_TEST_PTE_DIRTY (1ULL << 2)
#define CMDP_MON_TEST_PTE_SAVED_DIRTY (1ULL << 3)
#define CMDP_MON_TEST_PTE_PROTNONE (1ULL << 4)
#define CMDP_MON_TEST_PTE_MASK ((1ULL << 5) - 1)
struct cmdp_monitor_test_pte {
	__u32 size, version;
	__u64 flags;
	__u32 supported, dirty;
	__u64 cleared_flags;
};
#define CMDP_MON_TEST_PTE _IOWR(CMDP_IOC_MAGIC, 31, struct cmdp_monitor_test_pte)

/* CONFIG_CMDP_TEST only, one-shot injection for the chunk covering addr.
 * Configure while ATTACHED (no asynchronous worker). A bounded pause occurs
 * after reset/protection but before the shared flush. abort_after additionally
 * aborts an ARM batch after N PTEs were protected; all are flushed/cleaned up.
 * The root-only debugfs test_batch_waiting gauge exposes the paused phase.
 */
#define CMDP_MON_TEST_BATCH_DIRTY 1
#define CMDP_MON_TEST_BATCH_ARM 2
struct cmdp_monitor_test_batch {
	__u32 size, version, phase, delay_ms;
	__u64 addr;
	__u32 abort_after, reserved;
};
#define CMDP_MON_TEST_BATCH _IOW(CMDP_IOC_MAGIC, 32, struct cmdp_monitor_test_batch)

/* Configure after ATTACH and before the first scan. Both values are page
 * ceilings in [1, 65536]; zero selects 64. A shared flush drains both groups
 * when either ceiling, the bounded address window, or the time target ends.
 * They are not the architecture's individual-invalidation threshold.
 */
#define CMDP_BATCH_DEFAULT 64U
#define CMDP_BATCH_MAX 65536U
struct cmdp_monitor_batch_config {
	__u32 size, version, dirty_batch_pages, wp_batch_pages;
	__u64 reserved[2];
};
struct cmdp_monitor_batch_status {
	__u32 size, version, dirty_batch_pages, wp_batch_pages;
	__u64 now_ns, hold_target_ns;
	/* Actual shared calls/time: count each architectural flush exactly once.
	 * Legacy dirty/ARM per-role counters can overlap for a shared flush.
	 */
	__u64 flush_calls, shared_flush_calls, dirty_only_flush_calls, wp_only_flush_calls;
	__u64 flush_ns, dirty_pages, wp_pages;
	__u64 max_dirty_pages, max_wp_pages, max_span_pages;
	__u64 chunks, limit_chunks, time_chunks, max_hold_ns;
	__u64 region_lookups, region_full_skips, region_skip_pages, active_run_skips;
	__u64 history_regions, history_allocated_bytes, history_payload_bytes;
	__u64 pmd_walks, pmd_cache_hits;
};
#define CMDP_MON_BATCH_CONFIG _IOW(CMDP_IOC_MAGIC, 33, struct cmdp_monitor_batch_config)
#define CMDP_MON_BATCH_STATUS _IOWR(CMDP_IOC_MAGIC, 34, struct cmdp_monitor_batch_status)

#endif /* _UAPI_LINUX_CMDP_H */
