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

#define CMDP_MON_ATTACH _IOW(CMDP_IOC_MAGIC, 16, struct cmdp_monitor_attach)
#define CMDP_MON_START _IO(CMDP_IOC_MAGIC, 17)
#define CMDP_MON_SCAN_ONCE _IO(CMDP_IOC_MAGIC, 18)
#define CMDP_MON_STATUS _IOWR(CMDP_IOC_MAGIC, 19, struct cmdp_monitor_status)
#define CMDP_MON_STOP _IO(CMDP_IOC_MAGIC, 20)
#define CMDP_MON_PAGE _IOWR(CMDP_IOC_MAGIC, 21, struct cmdp_monitor_page)
#define CMDP_MON_COHORT _IOWR(CMDP_IOC_MAGIC, 22, struct cmdp_monitor_cohort)

#endif /* _UAPI_LINUX_CMDP_H */
